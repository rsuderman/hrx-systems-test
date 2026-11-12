#!/usr/bin/env python3
"""Upgrade HRX 0.1.0 pipeline source contracts for HRX 0.2.0.

Never edits the input or repairs publication locks.

Use --check to preview, or --output NEW_DIRECTORY to create a local derivative.
Input must be a publication/source tree or a prepared tree with linked assets.
"""
import argparse
import copy
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile


MIGRATION_ID = 'hrx-pipelines-0.1.0-to-0.2.0'
IMPLEMENTATION_REVISION = 1
REPORT_SCHEMA_VERSION = 1
MAX_DIFF_BYTES = 64 * 1024
MAX_RECORDED_EDITS = 1024
WEIGHT_SUFFIXES = {'.gguf', '.safetensors', '.bin'}

OP = re.compile(r'\bvector\.constant\b')
LITERAL = re.compile(r'vector\.constant\s+(?P<value>\S+)\s*:\s*vector<(?P<type>[^<>]+)>\s*$')
INTEGER = re.compile(r'[+-]?(?:[0-9]+|0[xX][0-9a-fA-F]+)\Z')
FLOAT = re.compile(r'[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|inf|nan)\Z')
COMMAND = re.compile(r'(?<![@%\w.])command\.program\.[\w.]+')
COMMAND_NAMES = {f'command.program.{kind}': f'command.{kind}' for kind in ('def', 'decl', 'launch')}
POOL_RANGE = re.compile(r'config\.decl\s+@llm\.pool\s*:\s*%(?P<arg>[\w.$-]+)\s*:\s*index\s+where\s+\[range\(\s*%(?P=arg)\s*,\s*(?P<minimum>[0-9]+)\s*,\s*(?P<maximum>[0-9]+)\s*\)\]')
CHAT_PARAMS = '%raw: buffer, %role: buffer, %content: buffer, %parts: buffer, %reasoning: buffer, %calls: buffer, %state: i64, %ordinal: i64, %fields: i64, %part_count: i64'
CHAT_VALUES = '%raw, %role, %content, %parts, %reasoning, %calls, %state, %ordinal, %fields, %part_count'
CHAT_TYPES = 'buffer, buffer, buffer, buffer, buffer, buffer, i64, i64, i64, i64'
CHAT_RETURNS = 'buffer, i64, buffer'
# Verified legacy providers reject all fields/parts and return no projections.
# The second family deliberately omits the message suffix (Flint template).
CHAT_PROVIDER_HASHES = {
    '3cd4154ae51c0f83d682f049519b99d6ca5a171cffb08a959105c61e6f11d2ee',
    '87bcf9802d7e905040d5edad8ff2ccc8c2cc8a3117695b81ac5f4df5e391d38b',
}


def function_block(text, name):
    if '@' + name + '(' not in text:
        return None
    code, in_block = [], False
    for line in text.splitlines(keepends=True):
        masked, in_block = masked_line(line, in_block)
        if line.endswith('\n'):
            masked = masked[:-1] + '\n'
        code.append(masked)
    code = ''.join(code)
    match = re.search(r'^func\.def public @' + re.escape(name) + r'\([^\n]*\{\n', code, re.M)
    if not match:
        return None
    depth = 1
    for token in re.finditer(r'[{}]', code[match.end():]):
        depth += 1 if token.group() == '{' else -1
        if depth == 0:
            return text[match.start():match.end() + token.end()]
    raise ValueError(f'unterminated provider {name}')


def legacy_service_updates(programs):
    """Migrate only a complete, verified text-only provider/wrapper closure."""
    old_header = f'func.def public @chat_message({CHAT_PARAMS}) -> ({CHAT_RETURNS}) {{'
    legacy_export = f'func.def public @text_chat_message_v1({CHAT_PARAMS})'
    if not any((function_block(text, 'chat_message') or '').startswith(old_header) or
               (function_block(text, 'text_chat_message_v1') or '').startswith(legacy_export)
               for text in programs.values()):
        return {}
    providers = [(path, function_block(text, 'chat_message')) for path, text in programs.items()]
    providers = [(path, block) for path, block in providers if block and block.startswith(old_header)]
    if len(providers) != 1 or sha(providers[0][1].encode()) not in CHAT_PROVIDER_HASHES:
        raise ValueError('legacy chat provider is not a verified text-only family; migrate media semantics manually')
    path, provider = providers[0]
    new_params = CHAT_PARAMS.replace('%parts: buffer, ', '%parts: buffer, %frames: buffer, ')
    new_values = CHAT_VALUES.replace('%parts, ', '%parts, %frames, ')
    new_types = 'buffer, ' + CHAT_TYPES
    new_returns = CHAT_RETURNS + ', i64'
    new_provider = provider.replace(old_header, f'func.def public @chat_message({new_params}) -> ({new_returns}) {{', 1)
    new_provider = new_provider.replace('  %c0 = index.constant 0 : offset\n',
        '  %c0 = index.constant 0 : offset\n'
        '  %fixup_frame_bytes = buffer.length %frames\n'
        '  %fixup_no_frames = index.cmp eq, %fixup_frame_bytes, %c0 : offset\n'
        '  func.call @check(%fixup_no_frames) : (i1)\n', 1)
    new_provider = new_provider.replace('func.return %fragment, %state, %empty : buffer, i64, buffer',
                                        'func.return %fragment, %state, %empty, %c0_i64 : buffer, i64, buffer, i64', 1)
    replacements = [(provider, new_provider, 'chat provider: reject frames, return zero projection count')]
    replacements.append((f'func.decl @chat_message({CHAT_PARAMS}) -> ({CHAT_RETURNS})',
                         f'func.decl @chat_message({new_params}) -> ({new_returns})', 'chat provider declaration'))
    for suffix in ('', '_v1'):
        name = 'text_chat_message' + suffix
        old = (f'func.def public @{name}({CHAT_PARAMS}) -> ({CHAT_RETURNS}) {{\n'
               f'  %result_0, %result_1, %result_2 = func.call @chat_message({CHAT_VALUES}) : ({CHAT_TYPES}) -> ({CHAT_RETURNS})\n'
               '  func.return %result_0, %result_1, %result_2 : buffer, i64, buffer\n}')
        new = (f'func.def public @{name}({new_params}) -> ({new_returns}) {{\n'
               f'  %result_0, %result_1, %result_2, %result_3 = func.call @chat_message({new_values}) : ({new_types}) -> ({new_returns})\n'
               '  func.return %result_0, %result_1, %result_2, %result_3 : buffer, i64, buffer, i64\n}')
        replacements.append((old, new, name + ' wrapper'))
    old_tool_params = '%schemas: buffer, %response: buffer'
    new_tool_params = old_tool_params + ', %truncated: i64'
    old_tool = (f'func.def public @parse_tools({old_tool_params}) -> (buffer) {{\n'
                '  %empty = global.load @no_tools : buffer\n  func.return %empty : buffer\n}')
    replacements.append((old_tool, old_tool.replace(old_tool_params, new_tool_params, 1),
                         'no-tools provider: same empty result for EOS and truncation'))
    replacements.append((f'func.decl @parse_tools({old_tool_params}) -> (buffer)',
                         f'func.decl @parse_tools({new_tool_params}) -> (buffer)', 'tool parser declaration'))
    for suffix in ('', '_v1'):
        name = 'text_parse_tools' + suffix
        old = (f'func.def public @{name}({old_tool_params}) -> (buffer) {{\n'
               '  %result_0 = func.call @parse_tools(%schemas, %response) : (buffer, buffer) -> (buffer)\n'
               '  func.return %result_0 : buffer\n}')
        new = (f'func.def public @{name}({new_tool_params}) -> (buffer) {{\n'
               '  %result_0 = func.call @parse_tools(%schemas, %response, %truncated) : (buffer, buffer, i64) -> (buffer)\n'
               '  func.return %result_0 : buffer\n}')
        replacements.append((old, new, name + ' wrapper'))
    updates = {}
    for old, new, label in replacements:
        locations = [(p, text.count(old)) for p, text in programs.items() if old in text]
        if sum(count for _, count in locations) != 1:
            raise ValueError('incomplete or unfamiliar legacy provider closure: ' + label)
        p = locations[0][0]
        text, edits = updates.get(p, (programs[p], []))
        line = programs[p][:programs[p].index(old)].count('\n') + 1
        updates[p] = (text.replace(old, new, 1), edits + [{'line': line, 'kind': 'text_service_abi', 'change': label}])
    return updates


def legacy_decision_updates(programs):
    """Adapt the verified one-shot d1 provider to retained finite batches."""
    header = 'func.def public @decision_prepare_batch_v1(%state: buffer, %tags: buffer, %ranges: buffer, %packed: buffer)'
    if not any(header in text for text in programs.values()):
        return {}
    families = {
        'control': 'b8a42f7c13f348c645e30a18eedff6457dd175061a4263e0e599b48f81915c28',
        'service': '6edbfff144ebdd90e63eff0f18ba810ae9f0cc1ec736d44e8edd6c63d2fd2a60',
    }
    found = {}
    for role, digest in families.items():
        matches = [(p, text) for p, text in programs.items() if sha(text.encode()) == digest]
        if len(matches) != 1:
            raise ValueError('unfamiliar or incomplete legacy decision provider; review finite batch semantics manually')
        found[role] = matches[0]
    batch_type = 'type.decl import("finite", "batch") @FixupFiniteBatch\n\n'
    updates = {}
    for role, (path, text) in found.items():
        old_batch = '%state: buffer, %tags: buffer, %ranges: buffer, %packed: buffer'
        new_batch = '%state: buffer, %tags: buffer, %batch: ptr<@FixupFiniteBatch>'
        text = text.replace(old_batch, new_batch)
        old_run = '%stage: i32, %input: hal.buffer, %output: hal.buffer'
        new_run = '%state: buffer, ' + old_run + ', %batch: ptr<@FixupFiniteBatch>'
        text = text.replace('@run(' + old_run, '@run(' + new_run)
        text = text.replace('@decision_run_v1(' + old_run, '@decision_run_v1(' + new_run)
        if role == 'control':
            text = text.replace('-> (i32, buffer, buffer) {\n',
                '-> (i32, buffer, buffer) {\n'
                '  %ranges, %packed = func.call @fixup_batch_pack(%batch) : (ptr<@FixupFiniteBatch>) -> (buffer, buffer)\n', 1)
            for helper, width in (('byte_at', 1), ('i32_at', 4), ('i64_at', 8),
                                  ('i32_b', 4), ('i64_b', 8), ('set_i32', 4), ('set_i64', 8)):
                start = text.index('func.def @' + helper + '(')
                end = text.index('\n}', start) + 2
                block = text[start:end]
                block = block.replace('  %v = buffer.view %buf[%o]',
                    f'  %fixup_width = index.constant {width} : offset\n'
                    '  %fixup_o = func.call @fixup_checked_offset(%buf, %o, %fixup_width) : (buffer, offset, offset) -> (offset)\n'
                    '  %v = buffer.view %buf[%fixup_o]', 1)
                text = text[:start] + block + text[end:]
            text += '''
func.def @fixup_checked_offset(%buf: buffer, %position: offset, %width: offset) -> (offset) {
  %extent = buffer.length %buf
  %wide_enough = index.cmp uge, %extent, %width : offset
  %last = index.sub %extent, %width : offset
  %in_bounds = index.cmp ule, %position, %last : offset
  %valid = scalar.andi %wide_enough, %in_bounds : i1
  %message = buffer.constant utf8("decision buffer access exceeds its extent") : buffer
  func.call @fail_unless(%valid, %message) : (i1, buffer)
  %bounded = index.assume %position [le(%position, %last)] : offset
  func.return %bounded : offset
}
'''
            text = (batch_type +
                'func.decl import("finite", "batch_pack") @fixup_batch_pack(%batch: ptr<@FixupFiniteBatch>) -> (buffer, buffer)\n'
                'func.decl import("finite", "batch_finish") @fixup_batch_finish(%batch: ptr<@FixupFiniteBatch>)\n\n' + text +
                '\nfunc.def public @collect_batch(%state: buffer, %batch: ptr<@FixupFiniteBatch>) {\n'
                '  func.call @fixup_batch_finish(%batch) : (ptr<@FixupFiniteBatch>)\n'
                '  func.return\n}\n')
        else:
            text = text.replace('@prepare_batch(%state, %tags, %ranges, %packed) : (buffer, buffer, buffer, buffer)',
                                '@prepare_batch(%state, %tags, %batch) : (buffer, buffer, ptr<@FixupFiniteBatch>)')
            text = text.replace('@run(%stage, %input, %output) : (i32, hal.buffer, hal.buffer)',
                                '@run(%state, %stage, %input, %output, %batch) : (buffer, i32, hal.buffer, hal.buffer, ptr<@FixupFiniteBatch>)')
            text = (batch_type + text +
                '\nfunc.decl @collect_batch(%state: buffer, %batch: ptr<@FixupFiniteBatch>)\n'
                'func.def public @decision_collect_batch_v1(%state: buffer, %batch: ptr<@FixupFiniteBatch>) {\n'
                '  func.call @collect_batch(%state, %batch) : (buffer, ptr<@FixupFiniteBatch>)\n'
                '  func.return\n}\n')
        updates[path] = (text, [{'line': 1, 'kind': 'decision_batch_abi', 'change': role + ': retained one-shot finite batch'}])
    return updates


def masked_line(line, in_block):
    """Hide comments and strings, retaining columns for precise token edits."""
    chars = list(line)
    i = 0
    while i < len(line):
        if in_block:
            end = line.find('*/', i)
            stop = len(line) if end < 0 else end + 2
            chars[i:stop] = ' ' * (stop - i)
            i = stop
            in_block = end < 0
        elif line.startswith('//', i):
            chars[i:] = ' ' * (len(line) - i)
            break
        elif line.startswith('/*', i):
            in_block = True
        elif line[i] == '"':
            start = i
            i += 1
            while i < len(line):
                if line[i] == '\\':
                    i += 2
                elif line[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
            chars[start:i] = ' ' * len(chars[start:i])
        else:
            i += 1
    return ''.join(chars), in_block


def migrate_program(text, name='<source>'):
    text, selection_edits = migrate_minicpm_reasoning(text, name)
    text, reasoning_edits = migrate_audio_output(text, name)
    text, convolution_edits = migrate_convolution_guard(text, name)
    text, weight_edits = migrate_weight_keys(text, name)
    output, edits, in_block = [], selection_edits + reasoning_edits + convolution_edits + weight_edits, False
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        block_before = in_block
        code, in_block = masked_line(line, in_block)
        # Upstream d835680: operation names changed, callable ABI did not.
        for match in reversed(list(COMMAND.finditer(code))):
            old = match.group()
            if old not in COMMAND_NAMES:
                raise ValueError(f'{name}:{number}: unsupported command operation {old!r}')
            replacement = COMMAND_NAMES[old]
            line = line[:match.start()] + replacement + line[match.end():]
            edits.append({'line': number, 'kind': 'command_operation', 'from': old, 'to': replacement})
        # Renaming can shift columns before a literal operation on the same line.
        code, _ = masked_line(line, block_before)
        matches = list(OP.finditer(code))
        if not matches:
            output.append(line)
            continue
        found = matches[0]
        literal = LITERAL.fullmatch(code[found.start():].strip())
        if len(matches) != 1 or not literal or not re.search(r'%[\w.$-]+\s*=\s*$', code[:found.start()]):
            raise ValueError(f'{name}:{number}: unsupported vector.constant syntax; review manually')
        element = literal['type'].rsplit('x', 1)[-1].strip()
        # index contains an x: recognize address element types explicitly.
        if literal['type'].endswith('xindex'):
            element = 'index'
        if re.fullmatch(r'i[0-9]+|index|offset', element):
            replacement, valid = 'vector.splati', INTEGER.fullmatch(literal['value'])
        elif element in ('f8E4M3', 'f8E5M2', 'f16', 'bf16', 'f32', 'f64'):
            replacement, valid = 'vector.splatf', FLOAT.fullmatch(literal['value'])
        else:
            raise ValueError(f'{name}:{number}: unsupported element type {element!r}')
        if not valid:
            raise ValueError(f'{name}:{number}: unsupported literal {literal["value"]!r}')
        output.append(line[:found.start()] + replacement + line[found.end():])
        edits.append({'line': number, 'kind': 'vector_literal', 'from': 'vector.constant', 'to': replacement,
                      'element_type': element, 'literal': literal['value']})
    return ''.join(output), edits


def migrate_minicpm_reasoning(text, name):
    """Honor explicit reasoning_effort=none in the verified legacy MiniCPM template."""
    if 'global.rodata.def @name = bytes("333561335f68665f7634375f30393233")' not in text:
        return text, []
    if 'func.def @fixup_no_thinking(' in text:
        return text, []
    begin, end = function_block(text, 'chat_begin'), function_block(text, 'chat_end')
    if (not begin or not end or
        sha(begin.encode()) != '09ef667688c344f056876938bdc1e0bbec7b5fe91bc57fd3079ace976bd6c870' or
        sha(end.encode()) != '62aa4ddccd0577be8d5b38bebfcd425392f4d1af024b1cf240070b2acd36508e' or
        'global.rodata.def @generation_prompt = bytes("3c7c696d5f73746172747c3e617373697374616e740a3c7468696e6b3e0a")' not in text):
        raise ValueError(f'{name}: unfamiliar legacy MiniCPM reasoning template; review manually')
    new_begin = begin.replace('  func.return %empty, %c0_i64 : buffer, i64',
        '  %fixup_disabled = func.call @fixup_no_thinking(%request) : (buffer) -> (i64)\n'
        '  func.return %empty, %fixup_disabled : buffer, i64')
    new_end = '''func.def public @chat_end(%state: i64) -> (buffer, i64) {
  %zero = scalar.constant 0 : i64
  %one = scalar.constant 1 : i64
  %disabled = scalar.cmpi ne, %state, %zero : i64
  %default_prompt = global.load @generation_prompt : buffer
  %plain_prompt = global.load @fixup_generation_plain : buffer
  %prompt = scf.select %disabled, %plain_prompt, %default_prompt : buffer
  %thinking = scf.select %disabled, %zero, %one : i64
  func.return %prompt, %thinking : buffer, i64
}'''
    helpers = '''
global.rodata.def @fixup_effort_key = bytes("726561736f6e696e675f6566666f7274")
global.rodata.def @fixup_effort_none = bytes("6e6f6e65")
global.rodata.def @fixup_generation_plain = bytes("3c7c696d5f73746172747c3e617373697374616e740a3c7468696e6b3e0a0a3c2f7468696e6b3e0a0a")

func.def @fixup_json_text(%source: buffer, %begin: i64, %length: i64) -> (buffer) {
  %zero64 = scalar.constant 0 : i64
  %zero = index.constant 0 : offset
  %bounded_length = scalar.assume %length [range(%length, 0, 9223372036854775807)] : i64
  %size = index.cast %bounded_length : i64 to offset
  %storage = buffer.alloca<private> align(1) %size : buffer
  %decoded = func.call @unescape(%source, %begin, %length, %storage, %zero64) : (buffer, i64, i64, buffer, i64) -> (i64)
  %bounded_decoded = scalar.assume %decoded [range(%decoded, 0, 9223372036854775807)] : i64
  %extent = index.cast %bounded_decoded : i64 to offset
  %result = buffer.alloca<private> align(1) %extent : buffer
  buffer.copy %storage[%zero], %result[%zero], %extent
  func.return %result : buffer
}

func.def @fixup_no_thinking(%request: buffer) -> (i64) {
  %zero = index.constant 0 : index
  %one = index.constant 1 : index
  %origin = index.constant 0 : offset
  %zero64 = scalar.constant 0 : i64
  %one64 = scalar.constant 1 : i64
  %false = scalar.constant false : i1
  %key = global.load @fixup_effort_key : buffer
  %none = global.load @fixup_effort_none : buffer
  %fields, %type, %count = func.call @members(%request) : (buffer) -> (buffer, i32, i64)
  %bounded = scalar.assume %count [range(%count, 0, 2147483647)] : i64
  %extent = index.cast %bounded : i64 to index
  %records = buffer.view %fields[%origin] : buffer -> view<[%extent]x5xi64>
  %disabled = scf.for %i = [%zero to %extent step %one](%off = %false : i1) -> (i1) {
    %kb = view.load %records[%i, 0] : view<[%extent]x5xi64> -> i64
    %kl = view.load %records[%i, 1] : view<[%extent]x5xi64> -> i64
    %name = func.call @fixup_json_text(%request, %kb, %kl) : (buffer, i64, i64) -> (buffer)
    %match = func.call @equal(%name, %key) : (buffer, buffer) -> (i1)
    %vt = view.load %records[%i, 4] : view<[%extent]x5xi64> -> i64
    %string = scalar.cmpi eq, %vt, %zero64 : i64
    %usable = scalar.andi %match, %string : i1
    %next = scf.if %usable -> (i1) {
      %vb = view.load %records[%i, 2] : view<[%extent]x5xi64> -> i64
      %vl = view.load %records[%i, 3] : view<[%extent]x5xi64> -> i64
      %value = func.call @fixup_json_text(%request, %vb, %vl) : (buffer, i64, i64) -> (buffer)
      %is_none = func.call @equal(%value, %none) : (buffer, buffer) -> (i1)
      scf.yield %is_none : i1
    } else {
      scf.yield %off : i1
    }
    scf.yield %next : i1
  }
  %result = scf.select %disabled, %one64, %zero64 : i64
  func.return %result : i64
}
'''
    changed = text.replace(begin, new_begin, 1).replace(end, new_end, 1) + helpers
    return changed, [{'line': text[:text.index(begin)].count('\n') + 1, 'kind': 'reasoning_selection',
        'change': 'honor explicit reasoning_effort none; retain default open-think prompt'}]


def migrate_audio_output(text, name):
    """Recognize generated opening reasoning markers in verified LFM Audio output."""
    if 'global.rodata.def @name = bytes("4c464d3320417564696f203242")' not in text:
        return text, []
    if 'func.def @fixup_original_output_next(' in text:
        return text, []
    block = function_block(text, 'output_next')
    if not block or sha(block.encode()) != '6339f89abf441e14e27a43196b59aaed24214915720e50d75bf1c1bf8e2122d0':
        raise ValueError(f'{name}: unfamiliar audio reasoning output parser; review manually')
    if 'global.rodata.def @think_open = bytes("3c7468696e6b3e")' not in text:
        raise ValueError(f'{name}: unfamiliar audio reasoning opening marker')
    legacy = block.replace('func.def public @output_next(', 'func.def @fixup_original_output_next(', 1)
    wrapper = '''
func.def public @output_next(%response: buffer, %previous: i64, %state: i64, %phase: i32) -> (i64, i64, i64, i64) {
  %zero = scalar.constant 0 : i64
  %one = scalar.constant 1 : i64
  %content = scalar.cmpi eq, %state, %zero : i64
  %legacy_kind, %legacy_begin, %legacy_end, %legacy_next = func.call @fixup_original_output_next(%response, %previous, %state, %phase) : (buffer, i64, i64, i32) -> (i64, i64, i64, i64)
  %kind, %begin, %end, %next = scf.if %content -> (i64, i64, i64, i64) {
    %marker = global.load @think_open : buffer
    %open_end = func.call @text_until(%response, %marker, %previous, %phase) : (buffer, buffer, i64, i32) -> (i64)
    %stop = scalar.minsi %open_end, %legacy_end : i64
    %at_marker = scalar.cmpi eq, %stop, %previous : i64
    %open_first = scalar.cmpi eq, %stop, %open_end : i64
    %length_offset = buffer.length %response
    %length = index.cast %length_offset : offset to i64
    %marker_offset = buffer.length %marker
    %marker_length = index.cast %marker_offset : offset to i64
    %remaining = scalar.subi %length, %stop : i64
    %complete = scalar.cmpi uge, %remaining, %marker_length : i64
    %at_open = scalar.andi %at_marker, %open_first : i1
    %consume = scalar.andi %at_open, %complete : i1
    %after = scalar.addi %stop, %marker_length : i64
    %chosen_end = scf.select %consume, %after, %stop : i64
    %chosen_kind = scf.select %consume, %zero, %legacy_kind : i64
    %chosen_next = scf.select %consume, %one, %legacy_next : i64
    scf.yield %chosen_kind, %legacy_begin, %chosen_end, %chosen_next : i64, i64, i64, i64
  } else {
    scf.yield %legacy_kind, %legacy_begin, %legacy_end, %legacy_next : i64, i64, i64, i64
  }
  func.return %kind, %begin, %end, %next : i64, i64, i64, i64
}
'''
    line = text[:text.index(block)].count('\n') + 1
    return text.replace(block, legacy + wrapper, 1), [{'line': line, 'kind': 'reasoning_output',
        'change': 'consume complete opening think markers; preserve partial markers and legacy closing state'}]


def migrate_convolution_guard(text, name):
    """Make the token bound explicit in two verified legacy GDN helpers.

    Valid span metadata has 0 <= rel <= token. Thus rel >= back already
    implies token >= back. Express that redundant bound directly so one-token
    specialization discards the impossible projection-read branch before
    validating its index.assume. Keep the history branch and all bounds intact.
    """
    pattern = re.compile(r'^func\.def inline @g_gdn_conv_chan\([^\n]+\{\n.*?^}', re.M | re.S)
    old = '    %inside = index.cmp uge, %rel, %back : index'
    new = ('    %fixup_inside_rel = index.cmp uge, %rel, %back : index\n'
           '    %fixup_inside_token = index.cmp uge, %token, %back : index\n'
           '    %inside = scalar.andi %fixup_inside_rel, %fixup_inside_token : i1')
    known = {'4115dc4b2ad8c1e2f42c20abd26a64276811c63d9126b4da777f7a992b71f5f5',
             '803794d71d1dcd9c1771076d676e7fdd807847ee45f787cbb5068a7e441a047c'}
    edits, code_lines, in_comment = [], [], False
    for line in text.splitlines(keepends=True):
        code, in_comment = masked_line(line, in_comment)
        code_lines.append(code[:-1] + '\n' if line.endswith('\n') else code)
    for match in reversed(list(pattern.finditer(''.join(code_lines)))):
        block = text[match.start():match.end()]
        in_comment = False
        has_guard = False
        for line in block.splitlines():
            code, in_comment = masked_line(line, in_comment)
            has_guard |= code == old
        if not has_guard:
            continue
        if sha(block.encode()) not in known:
            raise ValueError(f'{name}: unfamiliar legacy convolution helper; review boundary semantics manually')
        changed = block.replace(old, new, 1)
        edits.append({'line': text[:match.start() + block.index(old)].count('\n') + 1,
                      'kind': 'gdn_convolution_guard',
                      'change': 'require token >= back alongside relative-position guard'})
        text = text[:match.start()] + changed + text[match.end():]
    return text, edits


def migrate_weight_keys(text, name):
    """Old whole-buffer keys become explicit [0, buffer.length) key ranges."""
    signatures = {
        'input': (['i32', 'buffer', 'i64', 'i64', 'i64'], ['i64']),
        'parameter': (['i32', 'buffer'], ['buffer', 'i64']),
    }
    declaration = re.compile(r'^\s*func\.decl\s+import\("weights"\)\s+@(?P<op>input|parameter)\((?P<params>[^()]*)\)\s*->\s*\((?P<results>[^()]*)\)')
    lines = text.splitlines(keepends=True)
    old, counts, changed_declarations = {}, {}, {}
    in_block = False
    for number, line in enumerate(lines):
        code, in_block = masked_line(line, in_block)
        if not code.lstrip().startswith('func.decl import('):
            continue
        match = declaration.match(line)
        if not match:
            continue
        op = match['op']
        counts[op] = counts.get(op, 0) + 1
        params = [p.strip() for p in match['params'].split(',')]
        types = [p.rsplit(':', 1)[-1].strip() for p in params]
        results = [p.strip() for p in match['results'].split(',')]
        expected_types, expected_results = signatures[op]
        new_types = expected_types[:2] + ['i64', 'i64'] + expected_types[2:]
        if results != expected_results or types not in (expected_types, new_types):
            raise ValueError(f'{name}:{number+1}: unfamiliar weights.{op} signature')
        if types == new_types:
            continue
        if any(not re.fullmatch(r'%[\w.$-]+\s*:\s*(i32|buffer|i64)', p) for p in params):
            raise ValueError(f'{name}:{number+1}: unsupported weight import parameters')
        if any(p.split(':', 1)[0].strip() in ('%key_offset', '%key_length') for p in params):
            raise ValueError(f'{name}:{number+1}: weight key parameter name collision')
        new_params = ', '.join(params[:2] + ['%key_offset: i64', '%key_length: i64'] + params[2:])
        changed_declarations[number] = line[:match.start('params')] + new_params + line[match.end('params'):]
        old[op] = (expected_types, expected_results, new_types)
    if any(count != 1 for count in counts.values()):
        raise ValueError(f'{name}: ambiguous duplicate weight import')
    if not old:
        return text, []
    if '%fixup_weight_key_' in text:
        raise ValueError(f'{name}: reserved weight fixup names already exist')
    call = re.compile(r'^(?P<indent>[ \t]*)(?:%[\w.$-]+(?:\s*,\s*%[\w.$-]+)*\s*=\s*)func\.call\s+@(?P<op>input|parameter)\((?P<args>[^()]*)\)\s*:\s*\((?P<types>[^()]*)\)\s*->\s*\((?P<results>[^()]*)\)\s*$')
    output, edits, in_block, ordinal = [], [], False, 0
    for number, line in enumerate(lines):
        code, in_block = masked_line(line, in_block)
        if number in changed_declarations:
            output.append(changed_declarations[number])
            edits.append({'line': number+1, 'kind': 'weight_key_range', 'change': 'import declaration'})
            continue
        used = [op for op in old if re.search(r'@' + op + r'(?![\w.])', code)]
        if not used:
            output.append(line)
            continue
        match = call.fullmatch(code)
        if not match or match['op'] not in old:
            raise ValueError(f'{name}:{number+1}: indirect or unfamiliar legacy weight import use')
        op = match['op']
        args = [arg.strip() for arg in match['args'].split(',')]
        types = [t.strip() for t in match['types'].split(',')]
        results = [t.strip() for t in match['results'].split(',')]
        expected_types, expected_results, new_types = old[op]
        if (types != expected_types or results != expected_results or len(args) != len(types) or
                any(not re.fullmatch(r'%[\w.$-]+', arg) for arg in args)):
            raise ValueError(f'{name}:{number+1}: unsupported legacy weight call')
        stem = f'%fixup_weight_key_'
        key_offset, key_extent, key_length = (f'{stem}{field}_{ordinal}' for field in ('offset', 'extent', 'length'))
        indent = match['indent']
        new_args = ', '.join(args[:2] + [key_offset, key_length] + args[2:])
        # Replace spans in reverse order so original comments/SSA results survive.
        updated = line[:match.start('types')] + ', '.join(new_types) + line[match.end('types'):]
        updated = updated[:match.start('args')] + new_args + updated[match.end('args'):]
        output.extend([f'{indent}{key_offset} = scalar.constant 0 : i64\n',
                       f'{indent}{key_extent} = buffer.length {args[1]}\n',
                       f'{indent}{key_length} = index.cast {key_extent} : offset to i64\n', updated])
        edits.append({'line': number+1, 'kind': 'weight_key_range', 'change': 'weights.' + op + ' call',
                      'key': args[1], 'key_range': 'whole buffer', 'data_arguments_preserved': args[2:]})
        ordinal += 1
    return ''.join(output), edits


def migrate_card(text):
    card = json.loads(text)
    if card.get('schema_version') != 1:
        raise ValueError('unsupported model card schema')
    after, edits = copy.deepcopy(card), []
    for name, preset in after.get('presets', {}).items():
        execution = preset.get('execution', {})
        if 'pool_capacity' in execution:
            edits.append({'path': ['presets', name, 'execution', 'pool_capacity'],
                          'removed_value': execution.pop('pool_capacity')})
    return (json.dumps(after, indent=2) + '\n' if edits else text), edits


def sha(data):
    return hashlib.sha256(data).hexdigest()


def validate_source_directories(source, card):
    """Never silently omit source hidden behind a directory link.

    rglob deliberately does not traverse linked directories. Reject those rather
    than publishing an unaudited namespace. Declared program paths additionally
    reveal unresolved directory links, which is important for container-relative
    links that cannot be stat'ed on the host. Individual asset-file links remain
    available to the copied tree and are never dereferenced here.
    """
    for path in source.rglob('*'):
        if path.is_symlink() and path.is_dir():
            raise ValueError('linked directories require manual source review')

    def paths(value, code=False):
        if isinstance(value, dict):
            if isinstance(value.get('path'), str):
                yield value['path'], code
            for key, child in value.items():
                if key != 'path':
                    yield from paths(child, key in ('inputs', 'libraries'))
        elif isinstance(value, list):
            for child in value:
                yield from paths(child, code)
        elif isinstance(value, str) and code:
            yield value, True

    for name, code in paths(card.get('programs', {})):
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('program paths must remain within the source tree')
        current = source
        for part in relative.parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise ValueError('linked source directories require manual review')
        if code and (source / relative).is_symlink():
            raise ValueError('linked program source requires manual review')


def plan(source):
    """Plan a derivative without writing; keys and identity paths are source-relative."""
    source = Path(source).resolve(strict=True)
    changes, report = {}, {'schema_version': REPORT_SCHEMA_VERSION, 'migration_id': MIGRATION_ID,
        'implementation_revision': IMPLEMENTATION_REVISION,
        'implementation_sha256': sha(Path(__file__).read_bytes()),
        'source_contract': '0.1.0', 'target_contract': '0.2.0',
        'source': str(source), 'files': [],
        'publication_ready': False, 'locks_modified': False,
        'source_pool_constraints': [],
        'note': 'Local derivative only; regenerate executable locks and publish through supported tooling.'}
    card = source / 'model.json'
    if not card.is_file() or card.is_symlink():
        raise ValueError('input requires a regular model.json')
    validate_source_directories(source, json.loads(card.read_text()))
    paths = [card, *sorted(source.rglob('*.loom'))]
    locks = sorted(source.rglob('*.lock'))
    if any(p.is_symlink() or not p.is_file() for p in locks):
        raise ValueError('publication locks must be regular files; review manually')
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise ValueError('linked or nonregular source requires manual review')
    identity_paths = sorted(set(paths + locks))
    source_data = {str(p.relative_to(source)): p.read_bytes() for p in identity_paths}
    report['source_files'] = [file_identity(name, data) for name, data in source_data.items()]
    programs = {p: source_data[str(p.relative_to(source))].decode('utf-8') for p in paths[1:]}
    service_updates = legacy_service_updates(programs)
    decision_updates = legacy_decision_updates(programs)
    if service_updates.keys() & decision_updates.keys():
        raise ValueError('overlapping service migrations require manual review')
    service_updates.update(decision_updates)
    for path in paths:
        if path.is_symlink():
            raise ValueError(f'linked source requires manual review: {path}')
        before = source_data[str(path.relative_to(source))]
        text = before.decode('utf-8')
        intermediate, service_edits = service_updates.get(path, (text, []))
        after, edits = migrate_card(text) if path == card else migrate_program(intermediate, str(path.relative_to(source)))
        edits = service_edits + edits
        if path != card:
            in_block = False
            for number, line in enumerate(text.splitlines(), 1):
                code, in_block = masked_line(line, in_block)
                constraint = POOL_RANGE.search(code)
                if constraint:
                    report['source_pool_constraints'].append({'path': str(path.relative_to(source)),
                        'line': number, 'symbol': 'llm.pool', 'minimum': int(constraint['minimum']),
                        'maximum': int(constraint['maximum']),
                        'action': 'preserved; shared backing policy must produce a value within this range'})
        if not edits:
            continue
        relative = str(path.relative_to(source))
        changes[relative] = after.encode('utf-8')
        diff = ''.join(difflib.unified_diff(text.splitlines(True), after.splitlines(True),
                                          'a/' + relative, 'b/' + relative))
        report['files'].append({'path': relative, 'before_sha256': sha(before),
            'after_sha256': sha(changes[relative]), 'edits': edits, **bounded_diff(diff)})
    report['pool_fields_removed'] = sum(len(f['edits']) for f in report['files'] if f['path'] == 'model.json')
    report['vector_operations_rewritten'] = sum(e.get('kind') == 'vector_literal' for f in report['files'] for e in f['edits'])
    report['command_operations_rewritten'] = sum(e.get('kind') == 'command_operation' for f in report['files'] for e in f['edits'])
    report['text_service_abi_edits'] = sum(e.get('kind') == 'text_service_abi' for f in report['files'] for e in f['edits'])
    report['weight_key_range_edits'] = sum(e.get('kind') == 'weight_key_range' for f in report['files'] for e in f['edits'])
    report['gdn_convolution_guard_edits'] = sum(e.get('kind') == 'gdn_convolution_guard' for f in report['files'] for e in f['edits'])
    report['decision_batch_abi_edits'] = sum(e.get('kind') == 'decision_batch_abi' for f in report['files'] for e in f['edits'])
    report['reasoning_output_edits'] = sum(e.get('kind') == 'reasoning_output' for f in report['files'] for e in f['edits'])
    report['reasoning_selection_edits'] = sum(e.get('kind') == 'reasoning_selection' for f in report['files'] for e in f['edits'])
    report['status'] = 'applied' if changes else 'unchanged'
    report['output_files'] = [file_identity(name, changes.get(name, data)) for name, data in source_data.items()]
    report['operation_counts'] = {'pool_capacity': report['pool_fields_removed'],
        **{kind: sum(e.get('kind') == kind for f in report['files'] for e in f['edits'])
           for kind in ('vector_literal', 'command_operation', 'text_service_abi',
                        'weight_key_range', 'gdn_convolution_guard', 'decision_batch_abi',
                        'reasoning_output', 'reasoning_selection')}}
    report['changed_file_count'] = len(changes)
    for record in report['files']:
        encoded = json.dumps(record['edits'], sort_keys=True, separators=(',', ':')).encode()
        record.update(edit_count=len(record['edits']), edits_sha256=sha(encoded),
                      edits_truncated=len(record['edits']) > MAX_RECORDED_EDITS,
                      edits_limit=MAX_RECORDED_EDITS)
        record['edits'] = record['edits'][:MAX_RECORDED_EDITS]
    return changes, report


def file_identity(name, data):
    return {'path': name, 'sha256': sha(data), 'size_bytes': len(data)}


def bounded_diff(diff):
    encoded = diff.encode('utf-8')
    truncated = len(encoded) > MAX_DIFF_BYTES
    if truncated:
        tail = '\n[diff truncated; full source identities remain recorded]\n'
        diff = encoded[:MAX_DIFF_BYTES - len(tail.encode())].decode('utf-8', errors='ignore') + tail
    return {'diff': diff, 'diff_sha256': sha(encoded), 'diff_size_bytes': len(encoded),
            'diff_truncated': truncated, 'diff_limit_bytes': MAX_DIFF_BYTES}


def write_derivative(source, output):
    """Write one audited derivative; never overwrite input, output or its receipt."""
    source, output = Path(source).resolve(strict=True), Path(output).absolute()
    changes, report = plan(source)
    report_path = output.with_name(output.name + '.upgrade.json')
    if any(p.exists() or p.is_symlink() for p in (output, report_path)):
        raise ValueError('output and adjacent upgrade report must not already exist')
    if output.resolve().is_relative_to(source) or source.is_relative_to(output.resolve()):
        raise ValueError('input and output trees must not overlap')
    # Do not dereference links or read checkpoint payloads to establish source identity.
    for path in source.rglob('*'):
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in WEIGHT_SUFFIXES:
            raise ValueError(f'link model assets before migrating; refusing to copy {path}')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.pipeline-upgrade-', dir=output.parent))
    reserved_output = created_report = False
    try:
        tree = temporary / 'model'
        shutil.copytree(source, tree, symlinks=True)
        for identity in report['source_files']:
            copied = tree / identity['path']
            if copied.is_symlink() or sha(copied.read_bytes()) != identity['sha256']:
                raise ValueError('source changed during migration')
        for relative, data in changes.items():
            (tree / relative).write_bytes(data)
        if plan(tree)[0]:
            raise ValueError('migration is not idempotent')
        report['output'] = str(output)
        # Reserve the destination exclusively before publishing. Replacing this empty
        # owned directory atomically cannot overwrite a preexisting output tree.
        output.mkdir()
        reserved_output = True
        with report_path.open('x') as stream:
            created_report = True
            json.dump(report, stream, indent=2)
            stream.write('\n')
        os.replace(tree, output)
        reserved_output = False
        return report, report_path
    except BaseException:
        if created_report:
            report_path.unlink(missing_ok=True)
        if reserved_output:
            # Remove only our empty reservation, never another writer's contents.
            try:
                output.rmdir()
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true', help='print a migration report without writing')
    mode.add_argument('--output', type=Path, help='new derivative directory; never the original')
    args = parser.parse_args()
    try:
        source = args.source.resolve(strict=True)
        if args.check:
            _, report = plan(source)
            print(json.dumps(report, indent=2))
            return 0
        report, report_path = write_derivative(source, args.output)
        print(f"{MIGRATION_ID}: {report['status']}; {report['changed_file_count']} changed files")
        print(f"Operation counts: {json.dumps(report['operation_counts'], sort_keys=True)}")
        print(f'Output: {args.output.absolute()}\nReport: {report_path}')
        return 0
    except (ValueError, OSError, UnicodeError) as error:
        parser.exit(1, f'Upgrade failed: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
