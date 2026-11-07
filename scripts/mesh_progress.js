/* Shared calculation for the global 3D job footer. Stage percentages are estimates;
   model counts and completion status come from the server. */
(function (root) {
  function meshProgress(job, assets, now, secondsPerModel = 90) {
    const steps = Object.fromEntries((job.steps || []).map(step => [step.key, step]));
    const selected = job.asset_ids?.length ? new Set(job.asset_ids) : null;
    const tracked = assets.filter(asset => !selected || selected.has(asset.asset_id));
    const total = selected ? selected.size : tracked.length;
    const ready = tracked.filter(asset => asset.status === 'mesh_complete' && asset.mesh_exists);
    const completed = ready.length;
    const terminal = ['succeeded', 'failed', 'cancelled'].includes(job.status);
    const time = terminal ? (job.ended_at || now) : now;
    const elapsed = Math.max(0, time - job.started_at);
    const perModel = Math.max(10, secondsPerModel);
    let percent = 0;
    let remaining = null;
    let stage = 'Preparing 3D generation';
    let overdue = false;
    const prep = [['stop_comfyui', 'Freeing GPU memory', 5, 5],
                  ['wait_vram', 'Waiting for GPU memory', 5, 10],
                  ['start_hunyuan', 'Loading Hunyuan3D', 5, 50]];
    const batch = steps.mesh_batch;
    const exporting = steps.game_assets;
    if (exporting?.status === 'running' || exporting?.status === 'done') {
      const spent = Math.max(0, time - exporting.updated_at);
      percent = 85 + 14 * Math.min(.95, spent / Math.max(15, total * 10));
      remaining = Math.max(15, total * 10) - spent;
      stage = 'Optimizing and exporting in Blender';
    } else if (batch?.status === 'running' || batch?.status === 'done') {
      // The last finished model's timestamp gives a better clock than the batch start.
      const latest = ready.map(asset => Date.parse(asset.updated_at) / 1000)
        .filter(value => Number.isFinite(value) && value >= batch.updated_at);
      const modelStart = Math.max(batch.updated_at, ...latest);
      const spent = Math.max(0, time - modelStart);
      const fraction = completed < total ? Math.min(.9, spent / perModel) : 0;
      percent = 15 + 70 * (total ? (completed + fraction) / total : 0);
      remaining = Math.max(0, total - completed) * perModel - (completed < total ? spent : 0) + 40;
      overdue = completed < total && spent > perModel * 1.5;
      stage = completed >= total && total ? 'Finalizing meshes and freeing GPU memory'
        : `Generating model ${Math.min(completed + 1, total) || 1} of ${total || '…'}`;
    } else {
      let pendingSeconds = 0;
      for (const [key, label, weight, duration] of prep) {
        const step = steps[key];
        if (step?.status === 'done') percent += weight;
        else if (step?.status === 'running') {
          const spent = Math.max(0, time - step.updated_at);
          percent += weight * Math.min(.9, spent / duration);
          pendingSeconds += Math.max(0, duration - spent);
          stage = label;
          overdue = spent > duration * 2;
        } else pendingSeconds += duration;
      }
      if (total) remaining = pendingSeconds + (total - completed) * perModel + 40;
    }
    if (job.status === 'stopping') stage = 'Stopping 3D generation';
    if (job.status === 'succeeded') { percent = 100; remaining = 0; stage = '3D models ready'; }
    if (job.status === 'failed') {
      stage = '3D generation failed';
      remaining = null;
    }
    if (job.status === 'cancelled') { stage = '3D generation cancelled'; remaining = null; }
    if (job.status === 'stopping') remaining = null;
    return {percent: job.status === 'succeeded' ? 100 : Math.min(99, Math.floor(percent)),
      elapsed, remaining: overdue || remaining === null || remaining < 0 ? null : Math.ceil(remaining),
      stage, completed, total, terminal};
  }
  if (typeof module !== 'undefined') module.exports = {meshProgress};
  else root.meshProgress = meshProgress;
})(typeof globalThis !== 'undefined' ? globalThis : this);
