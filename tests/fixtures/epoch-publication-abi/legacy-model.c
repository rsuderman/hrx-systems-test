// Copyright 2026 The IREE Authors
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
// Relevant function extracted from the pinned native source; see provenance.json.
iree_status_t loom_serve_text_model_complete_epoch(
    loom_serve_text_model_t* model, loom_serve_text_result_t* out_results) {
  const loom_serve_text_media_epoch_t* media = &model->invocation.media;
  iree_vm_buffer_t* media_plan =
      media->plan ? media->plan : model->control.host.buffers[TEXT_HOST_INPUT];
  iree_vm_variant_t publish_arguments[] = {
      iree_vm_buffer_variant_from_ptr_borrowed(
          &model->source.vm_types, model->control.host.buffers[TEXT_HOST_PLAN]),
      iree_vm_buffer_variant_from_ptr_borrowed(&model->source.vm_types,
                                               media_plan),
      iree_vm_variant_from_i32((int32_t)model->invocation.span_count),
      iree_vm_variant_from_i32((int32_t)media->first_feature_count),
      iree_vm_variant_from_i32((int32_t)media->next_feature_count),
      iree_vm_variant_from_i32(
          iree_any_bit_set(model->invocation.flags, TEXT_EPOCH_FLAG_VERIFY)),
      iree_vm_variant_from_i32(iree_any_bit_set(model->invocation.flags,
                                                TEXT_EPOCH_FLAG_CONTINUATION)),
      iree_vm_buffer_variant_from_ptr_borrowed(
          &model->source.vm_types,
          model->control.host.buffers[TEXT_HOST_CONTINUATIONS]),
      iree_vm_buffer_variant_from_ptr_borrowed(
          &model->source.vm_types,
          model->control.host.buffers[TEXT_HOST_OUTPUTS]),
      iree_vm_buffer_variant_from_ptr_borrowed(
          &model->source.vm_types,
          model->control.host.buffers[TEXT_HOST_RESULTS]),
      iree_vm_buffer_variant_from_ptr_borrowed(
          &model->source.vm_types,
          model->control.host.buffers[TEXT_HOST_NEXT_RESULTS]),
      iree_vm_buffer_variant_from_ptr_borrowed(
          &model->source.vm_types,
          model->control.host.buffers[TEXT_HOST_PROGRESS]),
  };
  IREE_RETURN_IF_ERROR(
      loom_serve_text_invoke(model, model->control.publish_epoch,
                             iree_vm_variant_span_from_array(publish_arguments),
                             iree_vm_variant_span_empty()));
  for (iree_host_size_t i = 0; i < model->invocation.span_count; ++i) {
    const uint8_t* record = model->control.host.bytes[TEXT_HOST_PROGRESS].data +
                            i * TEXT_PROGRESS_FIELD_COUNT * sizeof(int32_t);
    loom_serve_text_result_t result;
    uint32_t source_position = 0;
    loom_serve_text_epoch_plan_read_progress(record, &result, &source_position);
    if (out_results) {
      out_results[i] = result;
    }
    loom_serve_text_row_t* row = model->rows[model->invocation.row_indices[i]];
    row->state->position += result.consumed_count;
    row->state->source_position = source_position;
    loom_serve_text_state_row_trim(row->state);
    row->has_prediction = result.output_count != 0;
    if (row->has_prediction) {
      row->transfer.token = result.tokens[result.output_count - 1];
      row->transfer.progress[0] = (int32_t)result.output_count;
      row->transfer.progress[7] =
          row->transfer.token == model->source.eos_token;
    }
  }
  model->invocation.span_count = 0;
  loom_serve_text_media_epoch_deinitialize(&model->invocation.media);
  loom_runtime_residency_release(model->residency);
  return iree_ok_status();
}
