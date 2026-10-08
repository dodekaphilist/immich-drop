<script>
  import { Button, Text } from '@immich/ui';
  import { mdiCheck, mdiContentCopy, mdiOpenInNew } from '@mdi/js';
  import { t } from './i18n.svelte.js';
  import { u } from './paths.js';

  /** The link plus a scannable QR code, shown after creating a link and from the link list. */
  /** @type {{ url: string, hint?: string }} */
  let { url, hint = '' } = $props();

  let copied = $state(false);

  async function copy() {
    if (!navigator.clipboard) return;
    try {
      await navigator.clipboard.writeText(url);
      copied = true;
      setTimeout(() => (copied = false), 1500);
    } catch {}
  }
</script>

<div class="flex flex-col items-center gap-4">
  {#if hint}<Text size="small" color="muted" class="text-center">{hint}</Text>{/if}

  <div class="rounded-2xl border border-gray-200 bg-white p-4 shadow-md dark:border-white/10">
    <img src={u(`/api/qr?text=${encodeURIComponent(url)}`)} alt={t('create.qr')} width="200" height="200" class="block size-48 sm:size-52" />
  </div>

  <div class="w-full rounded-xl bg-gray-100 px-3 py-2 text-center dark:bg-white/10">
    <Text size="small" class="break-all font-mono">{url}</Text>
  </div>

  <div class="flex flex-wrap justify-center gap-2">
    <Button leadingIcon={copied ? mdiCheck : mdiContentCopy} onclick={copy}>{copied ? t('create.copied') : t('create.copy')}</Button>
    <Button variant="outline" leadingIcon={mdiOpenInNew} href={url} target="_blank" rel="noopener">{t('links.open')}</Button>
  </div>
</div>
