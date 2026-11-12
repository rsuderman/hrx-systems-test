# Migrate verified text-service provider closures

The newer host expects an additional frame-metadata input and projection-count
result from `text_chat_message_v1`, plus the truncation input to tool parsing.
The old providers fail binding with host 11 versus source 10 arguments.

Upstream changes establish the contracts:
[frame groups and projection counts](https://github.com/ROCm/hrx-system/commit/9cef5e4c2ac5c910db004925b93a56d22716d5a1)
and [tool truncation](https://github.com/ROCm/hrx-system/commit/35b263a391faf84ea06ae87a316f5778d5991090).

The upgrader recognizes two exact legacy text-only provider bodies, including the
Flint family that intentionally omits a suffix. It updates the provider,
declaration and both versioned/unversioned wrappers together. These providers
already reject parts/tools and return an empty media plan; the migration explicitly
rejects frames and returns zero projections. The verified no-tools parser retains
its empty result for both normal termination and truncation.

Unknown providers, media semantics or incomplete wrapper closures stop before
output is published. This is not a generic multimodal adaptation. Tests cover
complete closure, suffix preservation, idempotence and rejection of altered media
or missing exports. Local qualification cleared five binding failures and exposed
separate response-contract issues in two models; those are separate repairs.
