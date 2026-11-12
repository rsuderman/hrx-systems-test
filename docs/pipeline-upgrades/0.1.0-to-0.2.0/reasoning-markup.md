# Recognize generated opening reasoning markers

LFM3 Audio starts generation in answer state, but its legacy output parser can
recognize `</think>` only after it has entered reasoning state. It never detects
a generated opening `<think>`, so thought text and delimiters appear in final
content. Its tokenizer already preserves these delimiters as nonspecial tokens.

For the exact verified LFM Audio name, parser body and opening marker, the upgrader
keeps the old parser as a private helper and adds an answer-state wrapper. It
consumes complete opening markers without output, enters reasoning state and uses
the existing closing/whitespace transitions afterward. The incremental scanner
holds partial prefixes during streaming, releases incomplete prefixes at finalization,
preserves text before a marker and respects an earlier tool marker.

No arbitrary text is stripped, no digit is extracted and generation is unchanged.
Unknown parser bodies/markers require review. The optional native test runs 21
boundary cases and requires the legacy version to fail before the upgraded version
passes. Standard-library tests cover scope, rejection and idempotence.

Historical full-model validation at publication
`2e04065815f5184c8c1d64eab19fe7a036aaf607` returned final `4` and separate reasoning
in both cycles, with the same 39 completion tokens as before the repair.
