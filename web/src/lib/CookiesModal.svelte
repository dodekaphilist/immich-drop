<script>
  import { onMount } from 'svelte';
  import {
    Button, Field, IconButton, Select, Table, TableBody, TableCell, TableHeader, TableHeading, TableRow, Text,
    Textarea, modalManager,
  } from '@immich/ui';
  import { mdiDelete, mdiPencil } from '@mdi/js';
  import { fetchJson, jsonRequest } from './config.svelte.js';
  import { u } from './paths.js';
  import { locale, t } from './i18n.svelte.js';
  import Dialog from './Dialog.svelte';

  /** @type {{ onClose: () => void }} */
  let { onClose } = $props();

  let cookies = $state([]);
  let platforms = $state([]);
  let platform = $state(undefined);
  let cookieString = $state('');
  let saving = $state(false);
  let note = $state({ kind: '', text: '' }); // feedback shown in the dialog
  const say = (kind, text) => (note = { kind, text });

  const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
  const platformOptions = $derived(platforms.map((p) => ({ label: cap(p), value: p })));

  function fmtDate(iso) {
    try {
      return new Date(iso).toLocaleDateString(locale(), { day: '2-digit', month: 'short', year: '2-digit' });
    } catch {
      return '-';
    }
  }

  async function load() {
    try {
      const { body } = await fetchJson('/api/cookies');
      cookies = (body && body.items) || [];
      platforms = (body && body.platforms) || [];
    } catch {
      cookies = [];
      platforms = [];
    }
  }

  onMount(load);

  async function save() {
    if (!platform) return say('danger', t('cookies.needPlatform'));
    if (!cookieString.trim()) return say('danger', t('cookies.needString'));
    saving = true;
    try {
      const { ok, body } = await jsonRequest('/api/cookies', { platform, cookie_string: cookieString.trim() });
      if (!ok) return say('danger', body.error || t('cookies.saveFailed'));
      say('success', t('cookies.saved', { platform }));
      platform = undefined;
      cookieString = '';
      await load();
    } catch (e) {
      say('danger', String(e.message || e));
    } finally {
      saving = false;
    }
  }

  async function remove(c) {
    const ok = await modalManager.showDialog({
      title: t('cookies.deleteTitle'),
      prompt: t('cookies.deletePrompt', { platform: c.platform }),
      confirmText: t('cookies.delete'),
      confirmColor: 'danger',
    });
    if (!ok) return;
    try {
      const r = await fetch(u(`/api/cookies/${c.platform}`), { method: 'DELETE' });
      if (!r.ok) throw new Error(t('cookies.saveFailed'));
      say('success', t('cookies.deleted', { platform: c.platform }));
      await load();
    } catch (e) {
      say('danger', String(e.message || e));
    }
  }

  function edit(c) {
    platform = c.platform;
    cookieString = c.cookie_string;
  }
</script>

<Dialog title={t('cookies.title')} size="large" {onClose}>
  <div class="flex flex-col gap-4">
    <Text size="small" color="muted">{t('cookies.help')}</Text>

    <div class="grid items-start gap-3 sm:grid-cols-[180px_1fr_auto]">
      <Field label={t('cookies.platform')}><Select options={platformOptions} placeholder={t('cookies.select')} bind:value={platform} /></Field>
      <Field label={t('cookies.string')}>
        <Textarea rows={2} placeholder="sessionid=abc123; csrftoken=xyz789; ..." class="font-mono text-sm" bind:value={cookieString} />
      </Field>
      <div class="sm:mt-7"><Button loading={saving} onclick={save}>{t('cookies.save')}</Button></div>
    </div>
    {#if note.text}<Text size="small" color={note.kind === 'success' ? 'success' : 'danger'}>{note.text}</Text>{/if}

    <div class="overflow-x-auto">
      <Table striped size="small">
        <TableHeader>
          <TableRow>
            <TableHeading>{t('cookies.platform')}</TableHeading>
            <TableHeading>{t('cookies.preview')}</TableHeading>
            <TableHeading>{t('cookies.updated')}</TableHeading>
            <TableHeading></TableHeading>
          </TableRow>
        </TableHeader>
        <TableBody>
          {#each cookies as c (c.platform)}
            <TableRow>
              <TableCell class="font-medium">{cap(c.platform)}</TableCell>
              <TableCell class="font-mono text-xs">{c.cookie_preview || (c.cookie_string ? c.cookie_string.slice(0, 40) + '...' : '')}</TableCell>
              <TableCell>{fmtDate(c.updated_at)}</TableCell>
              <TableCell>
                <div class="flex items-center gap-1">
                  <IconButton icon={mdiPencil} aria-label={t('cookies.edit')} size="small" variant="ghost" onclick={() => edit(c)} />
                  <IconButton icon={mdiDelete} aria-label={t('cookies.delete')} size="small" variant="ghost" color="danger" onclick={() => remove(c)} />
                </div>
              </TableCell>
            </TableRow>
          {:else}
            <TableRow><TableCell colspan={4}><Text color="muted" class="py-4 text-center">{t('cookies.none')}</Text></TableCell></TableRow>
          {/each}
        </TableBody>
      </Table>
    </div>
  </div>
</Dialog>
