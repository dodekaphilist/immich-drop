<script>
  import { onDestroy, onMount } from 'svelte';
  import { Badge, Button, Card, CardBody, IconButton, ProgressBar, Text } from '@immich/ui';
  import { mdiClose, mdiCloudUploadOutline } from '@mdi/js';
  import { formatBytes } from './config.svelte.js';
  import { t } from './i18n.svelte.js';
  import { UploadManager } from './upload-manager.svelte.js';

  /** @type {{ inviteToken?: string|null, disabled?: boolean }} */
  let { inviteToken = null, disabled = false } = $props();

  // The invite token is fixed for the lifetime of the page.
  // svelte-ignore state_referenced_locally
  const manager = new UploadManager({ inviteToken });

  let fileInput = $state(null);
  let dragOver = $state(false);
  let isTouch = $state(false);

  onMount(() => {
    isTouch = 'ontouchstart' in window || navigator.maxTouchPoints > 0;
    manager.start();
    // iOS Safari: `capture` forces camera-only and hides the photo library. Keep it on Android only.
    if (fileInput) {
      if (/Android/i.test(navigator.userAgent || '')) fileInput.setAttribute('capture', 'environment');
      else fileInput.removeAttribute('capture');
    }
  });
  onDestroy(() => manager.stop());

  let suppressClicksUntil = 0;

  function pick() {
    if (disabled) return;
    try {
      fileInput.value = '';
    } catch {}
    fileInput.click();
  }

  function onChange() {
    suppressClicksUntil = Date.now() + 800; // swallow stray clicks right after the picker closes
    manager.addFiles(fileInput.files);
    setTimeout(() => {
      try {
        fileInput.value = '';
      } catch {}
    }, 500);
  }

  function onDrop(e) {
    e.preventDefault();
    dragOver = false;
    if (!disabled) manager.addFiles(e.dataTransfer.files);
  }

  function zoneClick() {
    if (!isTouch && Date.now() >= suppressClicksUntil) pick();
  }

  const FINAL = ['done', 'duplicate', 'cancelled', 'error'];
  const BAR_COLOR = { done: 'success', duplicate: 'warning', error: 'danger', cancelled: 'secondary' };

  // The one line below the bar says where the upload stands; there is no second status anywhere.
  function statusText(it) {
    if (it.cancelling && !FINAL.includes(it.status)) return t('queue.cancelling');
    switch (it.status) {
      case 'queued':
        return t('s.queued');
      case 'sending':
        return it.pct >= 100 ? t('queue.waiting') : t('queue.sending', { pct: it.pct });
      case 'checking':
        return t('queue.checking');
      case 'uploading':
        return it.pct >= 100 ? t('queue.processing') : t('queue.toImmich', { pct: it.pct });
      case 'error':
        return it.message || t('s.error');
      default:
        return t('s.' + it.status);
    }
  }

  const c = $derived(manager.counts);
</script>

<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
<div
  class="flex flex-col items-center gap-3 rounded-2xl border-2 border-dashed p-10 text-center transition
    {dragOver ? 'border-primary bg-primary/10' : 'border-gray-300 dark:border-gray-600'}
    {disabled ? 'opacity-50' : 'cursor-pointer'}"
  ondragenter={(e) => { e.preventDefault(); dragOver = true; }}
  ondragover={(e) => { e.preventDefault(); dragOver = true; }}
  ondragleave={(e) => { e.preventDefault(); dragOver = false; }}
  ondrop={onDrop}
  onclick={zoneClick}
>
  <div class="hidden flex-col items-center gap-1 md:flex">
    <svg viewBox="0 0 24 24" width="48" height="48" class="text-primary" fill="currentColor"><path d={mdiCloudUploadOutline} /></svg>
    <Text fontWeight="medium">{t('drop.title')}</Text>
    <Text size="small" color="muted">{t('drop.or')}</Text>
  </div>
  <Button {disabled} onclick={(e) => { e.stopPropagation(); pick(); }}>{t('drop.choose')}</Button>
  <input bind:this={fileInput} type="file" multiple accept="image/*,video/*" class="sr-only" onchange={onChange} onclick={(e) => e.stopPropagation()} {disabled} />
</div>

<Card>
  <CardBody>
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div class="flex flex-wrap gap-2">
        <Badge color="secondary">{t('queue.queued')}: {c.queued}</Badge>
        <Badge color="primary">{t('queue.uploading')}: {c.uploading}</Badge>
        <Badge color="success">{t('queue.done')}: {c.done}</Badge>
        <Badge color="warning">{t('queue.duplicates')}: {c.duplicate}</Badge>
        <Badge color="danger">{t('queue.errors')}: {c.error}</Badge>
      </div>
      <div class="flex gap-2">
        <Button size="small" variant="outline" onclick={() => manager.clearFinished()}>{t('queue.clearFinished')}</Button>
        <Button size="small" variant="outline" onclick={() => manager.clearAll()}>{t('queue.clearAll')}</Button>
      </div>
    </div>
  </CardBody>
</Card>

<div class="flex flex-col gap-2">
  {#each manager.items as it (it.id)}
    <Card>
      <CardBody>
        <!-- fixed height, so nothing below moves when the state changes -->
        <div class="flex h-8 items-center justify-between gap-3">
          <div class="flex min-w-0 items-baseline gap-2">
            <Text class="truncate" title={it.name} fontWeight="medium">{it.name}</Text>
            <Text size="small" color="muted" class="shrink-0">({formatBytes(it.size)})</Text>
          </div>
          <!-- Always there, so the row keeps its height: cancels while running, removes the entry once finished -->
          <IconButton
            icon={mdiClose}
            size="small"
            variant="ghost"
            color="secondary"
            shape="round"
            class="shrink-0"
            aria-label={FINAL.includes(it.status) ? t('queue.remove') : t('queue.cancel')}
            disabled={it.cancelling && !FINAL.includes(it.status)}
            onclick={() => (FINAL.includes(it.status) ? manager.remove(it.id) : manager.cancel(it.id))}
          />
        </div>
        <div class="mt-2">
          <ProgressBar value={it.progress} max={100} type="progress" color={BAR_COLOR[it.status] ?? 'primary'} size="small" />
        </div>
        <Text size="small" color="muted" class="mt-1 min-h-5">{statusText(it)}</Text>
      </CardBody>
    </Card>
  {/each}
</div>
