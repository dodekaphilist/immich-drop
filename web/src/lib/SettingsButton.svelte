<script>
  import { Field, Icon, IconButton, Input, Select, Text, ThemePreference, Tooltip, themeManager } from '@immich/ui';
  import { mdiClose, mdiCog, mdiContentCopy, mdiContentSave, mdiDelete, mdiDownload, mdiInformationOutline, mdiOpenInNew, mdiPencil, mdiPlus, mdiRefresh } from '@mdi/js';
  import { LANGUAGES, i18n, t } from './i18n.svelte.js';
  import Dialog from './Dialog.svelte';
  import { fetchJson, jsonRequest } from './config.svelte.js';
  import { u } from './paths.js';
  import { apiKey, refreshKey } from './apikey.svelte.js';

  /** @type {{ owner?: boolean }} */
  let { owner = false } = $props();

  let open = $state(false);
  let editing = $state(false);
  let keyInput = $state('');
  let busy = $state(false);
  let error = $state('');
  let autoOpened = false;
  let shortcut = $state({ enabled: false, exists: false });
  let shortcutToken = $state(''); // shown once, right after it was generated

  const themeOptions = $derived([
    { label: t('theme.system'), value: ThemePreference.System },
    { label: t('theme.light'), value: ThemePreference.Light },
    { label: t('theme.dark'), value: ThemePreference.Dark },
  ]);

  // A key the user can not use (none yet, or rejected by Immich) is shown as a red dot on the cog
  const problem = $derived(owner && apiKey.loaded && (apiKey.status === 'missing' || apiKey.status === 'invalid'));
  const saved = $derived(apiKey.own && !editing);
  const locked = $derived(apiKey.server || saved);

  // First visit without any key: ask for it right away
  $effect(() => {
    if (owner && apiKey.loaded && apiKey.status === 'missing' && !autoOpened) {
      autoOpened = true;
      open = true;
    }
  });

  async function openDialog() {
    open = true;
    if (!owner) return;
    await refreshKey();
    try {
      const { ok, body } = await fetchJson('/api/me/shortcut');
      if (ok) shortcut = { enabled: !!body.enabled, exists: !!body.exists };
    } catch {}
  }

  function closeDialog() {
    open = false;
    editing = false;
    keyInput = '';
    error = '';
    shortcutToken = '';
  }

  async function save() {
    busy = true;
    error = '';
    try {
      const { ok, body } = await jsonRequest('/api/me/key', { apiKey: keyInput }, 'PUT');
      if (ok) {
        editing = false;
        keyInput = '';
        await refreshKey();
      } else {
        const known = ['key_invalid', 'key_other_account', 'key_missing_permission', 'key_managed_by_env', 'missing_key', 'immich_error'];
        error = t(`settings.${known.includes(body.error) ? body.error : 'keyFailed'}`, { status: body.status });
      }
    } catch {
      error = t('settings.keyFailed');
    } finally {
      busy = false;
    }
  }

  async function remove() {
    busy = true;
    error = '';
    try {
      await fetchJson('/api/me/key', { method: 'DELETE' });
      await refreshKey();
    } finally {
      busy = false;
    }
  }

  async function createShortcut() {
    busy = true;
    try {
      const { ok, body } = await jsonRequest('/api/me/shortcut', {});
      if (ok) {
        shortcutToken = body.token;
        shortcut.exists = true;
      }
    } finally {
      busy = false;
    }
  }

  async function deleteShortcut() {
    busy = true;
    try {
      const { ok } = await fetchJson('/api/me/shortcut', { method: 'DELETE' });
      if (ok) {
        shortcutToken = '';
        shortcut.exists = false;
      }
    } finally {
      busy = false;
    }
  }

  function downloadShortcut() {
    window.location.href = u('/api/me/shortcut/file');
  }

  function copyShortcut() {
    navigator.clipboard?.writeText(shortcutToken).catch(() => {});
  }

  function edit() {
    keyInput = '';
    editing = true;
  }

  function cancel() {
    keyInput = '';
    editing = false;
    error = '';
  }
</script>

<span class="relative inline-flex">
  <IconButton icon={mdiCog} shape="round" variant="ghost" size="small" aria-label={t('settings.open')} onclick={openDialog} />
  {#if problem}
    <span class="pointer-events-none absolute right-0 top-0 size-2.5 rounded-full bg-danger-500 ring-2 ring-white dark:ring-black"></span>
  {/if}
</span>

{#if open}
  <Dialog title={t('settings.title')} size="small" onClose={closeDialog}>
    <div class="flex flex-col gap-4">
      {#if owner}
        <div class="flex flex-col gap-1.5">
          <div class="flex items-center gap-1">
            <Text size="small" fontWeight="semi-bold">{t('settings.apiKey')}</Text>
            <Tooltip text={t('settings.keyPermissions')}>
              {#snippet child({ props })}
                <span {...props} role="img" aria-label={t('settings.keyPermissions')} class="inline-flex text-gray-500 dark:text-gray-400">
                  <Icon icon={mdiInformationOutline} size="16" />
                </span>
              {/snippet}
            </Tooltip>
            {#if apiKey.immichUrl && !apiKey.server}
              <IconButton
                href={`${apiKey.immichUrl}/user-settings?isOpen=api-keys`}
                target="_blank"
                rel="noopener noreferrer"
                icon={mdiOpenInNew}
                variant="ghost"
                shape="round"
                size="tiny"
                aria-label={t('settings.keyOpenImmich')}
                title={t('settings.keyOpenImmich')}
              />
            {/if}
          </div>
          <div class="flex items-center gap-1">
            <div class="min-w-0 grow rounded-lg {(problem || error) && !apiKey.server ? 'ring-2 ring-danger-500' : ''}">
              <Input
                type="text"
                autocomplete="off"
                autocapitalize="off"
                spellcheck="false"
                style="-webkit-text-security: disc"
                data-1p-ignore
                data-lpignore="true"
                data-bwignore
                data-form-type="other"
                disabled={locked}
                placeholder={apiKey.server || saved ? '••••••••••••••••' : t('settings.keyPlaceholder')}
                aria-label={t('settings.apiKey')}
                bind:value={keyInput}
              />
            </div>
            {#if !apiKey.server}
              {#if locked}
                <IconButton icon={mdiPencil} variant="ghost" shape="round" size="small" disabled={busy} aria-label={t('settings.keyEdit')} onclick={edit} />
                <IconButton icon={mdiDelete} variant="ghost" shape="round" size="small" color="danger" disabled={busy} aria-label={t('settings.keyDelete')} onclick={remove} />
              {:else}
                <IconButton icon={mdiContentSave} variant="ghost" shape="round" size="small" disabled={busy || !keyInput.trim()} aria-label={t('settings.keySave')} onclick={save} />
                {#if editing}
                  <IconButton icon={mdiClose} variant="ghost" shape="round" size="small" disabled={busy} aria-label={t('settings.keyCancel')} onclick={cancel} />
                {/if}
              {/if}
            {/if}
          </div>
          {#if apiKey.server}
            <Text size="small" color="muted">{t('settings.keyEnvHint')}</Text>
          {:else if error}
            <Text size="small" color="danger">{error}</Text>
          {:else if apiKey.status === 'invalid'}
            <Text size="small" color="danger">{t('settings.keyRejected')}</Text>
          {:else if apiKey.status === 'missing'}
            <Text size="small" color="danger">{t('settings.keyMissing')}</Text>
          {/if}
        </div>
        {#if shortcut.enabled}
          <div class="flex flex-col gap-1.5">
            <Text size="small" fontWeight="semi-bold">{t('settings.shortcut')}</Text>
            <div class="flex items-center gap-1">
              <div class="min-w-0 grow">
                <Input
                  type="text"
                  readonly
                  autocomplete="off"
                  spellcheck="false"
                  data-1p-ignore
                  data-lpignore="true"
                  data-bwignore
                  data-form-type="other"
                  disabled={!shortcutToken}
                  value={shortcutToken}
                  placeholder={shortcut.exists ? '••••••••••••••••' : ''}
                  aria-label={t('settings.shortcut')}
                />
              </div>
              {#if shortcutToken}
                <IconButton icon={mdiContentCopy} variant="ghost" shape="round" size="small" aria-label={t('settings.shortcutCopy')} onclick={copyShortcut} />
              {/if}
              <IconButton icon={mdiDownload} variant="ghost" shape="round" size="small" aria-label={t('settings.shortcutDownload')} onclick={downloadShortcut} />
              <IconButton
                icon={shortcut.exists ? mdiRefresh : mdiPlus}
                variant="ghost"
                shape="round"
                size="small"
                disabled={busy}
                aria-label={shortcut.exists ? t('settings.shortcutRenew') : t('settings.shortcutCreate')}
                onclick={createShortcut}
              />
              {#if shortcut.exists}
                <IconButton icon={mdiDelete} variant="ghost" shape="round" size="small" color="danger" disabled={busy} aria-label={t('settings.shortcutDelete')} onclick={deleteShortcut} />
              {/if}
            </div>
          </div>
        {/if}
        <hr class="border-gray-200 dark:border-gray-700" />
      {/if}
      <Field label={t('settings.theme')}>
        <Select
          options={themeOptions}
          value={themeManager.preference}
          onChange={(value) => themeManager.setPreference(value)}
        />
      </Field>
      <Field label={t('settings.language')}>
        <Select options={LANGUAGES} value={i18n.lang} onChange={(value) => i18n.set(value)} />
      </Field>
    </div>
  </Dialog>
{/if}
