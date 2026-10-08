<script>
  import { Button, Field, Icon, Input, Label, PasswordInput, Select, Text, Tooltip } from '@immich/ui';
  import { mdiCheckCircle, mdiInformationOutline } from '@mdi/js';
  import { jsonRequest } from './config.svelte.js';
  import { u } from './paths.js';
  import { t } from './i18n.svelte.js';
  import AlbumPicker from './AlbumPicker.svelte';
  import Dialog from './Dialog.svelte';
  import LinkShare from './LinkShare.svelte';

  /** @type {{ onClose: () => void, onCreated?: (token: string) => void }} */
  let { onClose, onCreated = () => {} } = $props();

  let name = $state('');
  let album = $state({ id: '', name: '' });
  let usage = $state('1');
  let days = $state('');
  let password = $state('');
  let creating = $state(false);
  let link = $state('');
  let error = $state('');

  const usageOptions = $derived([
    { label: t('create.once'), value: '1' },
    { label: t('create.unlimited'), value: '-1' },
  ]);

  async function create() {
    const payload = { maxUses: parseInt(usage, 10) };
    if (name.trim()) payload.name = name.trim();
    // A number input can hand back a number, not only a string
    const expiry = String(days ?? '').trim();
    if (expiry) {
      const n = Number(expiry);
      if (!Number.isInteger(n) || n < 1 || n > 3650) {
        error = t('create.invalidDays');
        return;
      }
      payload.expiresDays = n;
    }
    if (album.id) {
      payload.albumId = album.id;
      payload.albumName = album.name;
    } else if (album.name) {
      payload.albumName = album.name;
    }
    const pw = String(password ?? '').trim();
    if (pw) payload.password = pw;
    creating = true;
    error = '';
    try {
      const { ok, body } = await jsonRequest('/api/invites', payload);
      if (!ok) {
        error = body.error === 'no_api_key' ? t('create.noKey') : body.error === 'invalid_expiry' ? t('create.invalidDays') : t('create.failed');
        return;
      }
      link = body.absoluteUrl || location.origin + u(body.url);
      name = '';
      days = '';
      password = '';
      if (body.token) onCreated(body.token);
    } catch {
      error = t('create.failed');
    } finally {
      creating = false;
    }
  }
</script>

<Dialog title={t('home.share.title')} size="medium" {onClose}>
  {#if link}
    <div class="flex flex-col items-center gap-5 py-2">
      <div class="flex items-center gap-2 text-success-600 dark:text-success-400">
        <Icon icon={mdiCheckCircle} size="28" />
        <Text fontWeight="semi-bold" class="text-lg">{t('create.created')}</Text>
      </div>
      <LinkShare url={link} hint={t('create.shareHint')} />
      <Button variant="ghost" size="small" onclick={() => (link = '')}>{t('create.another')}</Button>
    </div>
  {:else}
    <div class="flex flex-col gap-4">
      <Field label={t('create.name')}>
        <Input placeholder={t('create.namePlaceholder')} maxlength={120} bind:value={name} />
      </Field>

      <AlbumPicker bind:value={album} />

      <div class="grid gap-3 sm:grid-cols-3">
        <Field label={t('create.usage')}><Select options={usageOptions} bind:value={usage} /></Field>
        <Field label={t('create.days')}><Input type="number" min="1" max="3650" step="1" placeholder={t('create.noExpiry')} bind:value={days} /></Field>
        <div class="flex w-full flex-col gap-1.5">
          <div class="flex items-center gap-1">
            <Label label={t('create.password')} size="small" />
            <Tooltip text={t('create.passwordHint')}>
              {#snippet child({ props })}
                <span {...props} role="img" aria-label={t('create.passwordHint')} class="inline-flex text-gray-500 dark:text-gray-400">
                  <Icon icon={mdiInformationOutline} size="16" />
                </span>
              {/snippet}
            </Tooltip>
          </div>
          <PasswordInput placeholder={t('create.setPassword')} autocomplete="new-password" data-1p-ignore data-lpignore="true" data-bwignore data-form-type="other" aria-label={t('create.password')} bind:value={password} />
        </div>
      </div>

      {#if error}<Text size="small" color="danger">{error}</Text>{/if}
      <div><Button loading={creating} onclick={create}>{t('create.submit')}</Button></div>
    </div>
  {/if}
</Dialog>
