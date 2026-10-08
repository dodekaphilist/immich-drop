<script>
  import { onMount } from 'svelte';
  import {
    Badge, Button, Card, CardBody, CardHeader, CardTitle, ContextMenuButton, Field, Icon, IconButton, Input, Select, Table, TableBody, TableCell, TableHeader,
    TableHeading, TableRow, Text, Tooltip, modalManager,
  } from '@immich/ui';
  import { mdiCheck, mdiContentCopy, mdiDelete, mdiInformationOutline, mdiLock, mdiOpenInNew, mdiPause, mdiPlay, mdiPlus, mdiQrcode } from '@mdi/js';
  import { fetchJson, formatBytes, jsonRequest } from './config.svelte.js';
  import { locale, t } from './i18n.svelte.js';
  import { u } from './paths.js';
  import Dialog from './Dialog.svelte';
  import LinkShare from './LinkShare.svelte';

  /** @type {{ highlight?: string|null, refreshKey?: number, onNew?: () => void }} */
  let { highlight = null, refreshKey = 0, onNew = () => {} } = $props();

  const sorts = $derived([
    { label: t('links.sort.newest'), value: '-created' },
    { label: t('links.sort.oldest'), value: 'created' },
    { label: t('links.sort.expiryDesc'), value: '-expires' },
    { label: t('links.sort.expiryAsc'), value: 'expires' },
    { label: t('links.sort.nameAsc'), value: 'name' },
    { label: t('links.sort.nameDesc'), value: '-name' },
  ]);

  let rows = $state([]);
  let loaded = $state(false);
  let q = $state('');
  let sort = $state('-created');
  let copiedToken = $state('');
  let flashToken = $state('');
  let qrUrl = $state('');
  let detail = $state(null); // { row, uploads, name, exp }
  let saving = $state(false);
  let error = $state(''); // list-level failures
  let detailError = $state('');

  const dayStr = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const today = dayStr(new Date());
  const maxDate = dayStr(new Date(Date.now() + 3650 * 864e5));
  const dateStr = (s) => (s ? new Date(s).toISOString().slice(0, 10) : '');
  const urlFor = (row) => `${location.origin}${u(`/invite/${row.token}`)}`;
  const isDisabled = (row) => /disabled/i.test(String(row.inactiveReason || ''));

  async function load() {
    const params = new URLSearchParams();
    if (q.trim()) params.set('q', q.trim());
    if (sort) params.set('sort', sort);
    try {
      const { body } = await fetchJson('/api/invites?' + params.toString());
      rows = (body && body.items) || [];
      error = '';
    } catch {
      rows = [];
    }
    loaded = true;
  }

  // Reload on search/sort changes (debounced) and when a new link was created.
  $effect(() => {
    q;
    sort;
    refreshKey;
    const timer = setTimeout(load, 250);
    return () => clearTimeout(timer);
  });

  $effect(() => {
    if (!highlight) return;
    flashToken = highlight;
    const timer = setTimeout(() => (flashToken = ''), 1600);
    return () => clearTimeout(timer);
  });

  onMount(load);

  function statusBadge(row) {
    const reason = String(row.inactiveReason || '').toLowerCase();
    if (/expired|claimed|exhausted/.test(reason)) return { color: 'danger', label: t('links.status.' + reason) };
    if (row.active) return { color: 'success', label: t('links.status.active') };
    if (/disabled/.test(reason)) return { color: 'warning', label: t('links.status.disabled') };
    return { color: 'secondary', label: t('links.status.inactive') };
  }

  // Row actions in priority order. As many as fit are shown as icons; the rest move into the three-dots
  // menu, last ones first (so "delete" is the first to leave the row).
  const BUTTON = 32; // px, IconButton size="small"
  const GAP = 4;
  let actionsW = $state(BUTTON * 5 + GAP * 4); // width of the actions cell, measured per row
  const rowActions = (row) => [
    { icon: copiedToken === row.token ? mdiCheck : mdiContentCopy, label: t('links.copy'), run: () => copy(row) },
    { icon: mdiQrcode, label: t('links.qr'), run: () => (qrUrl = urlFor(row)) },
    { icon: mdiOpenInNew, label: t('links.open'), run: () => window.open(urlFor(row), '_blank', 'noopener') },
    isDisabled(row)
      ? { icon: mdiPlay, label: t('links.enable'), run: () => toggle(row) }
      : { icon: mdiPause, label: t('links.disable'), run: () => toggle(row) },
    { icon: mdiDelete, label: t('links.delete'), color: 'danger', run: () => remove(row) },
  ];
  const ACTION_COUNT = 5;
  const inlineCount = $derived.by(() => {
    if (actionsW >= ACTION_COUNT * BUTTON + (ACTION_COUNT - 1) * GAP) return ACTION_COUNT; // everything fits, no menu
    // k icons + gap each + the menu button
    return Math.max(0, Math.floor((actionsW - BUTTON) / (BUTTON + GAP)));
  });
  const toMenuItem = (a) => ({ title: a.label, icon: a.icon, color: a.color, onAction: a.run });

  async function copy(row) {
    if (!navigator.clipboard) return;
    try {
      await navigator.clipboard.writeText(urlFor(row));
      copiedToken = row.token;
      setTimeout(() => (copiedToken = ''), 1000);
    } catch {}
  }

  async function toggle(row) {
    try {
      const { ok } = await jsonRequest('/api/invites/bulk', { tokens: [row.token], action: isDisabled(row) ? 'enable' : 'disable' });
      if (!ok) throw new Error();
      await load();
    } catch {
      error = t('links.updateFailed');
    }
  }

  async function remove(row) {
    const ok = await modalManager.showDialog({
      title: t('links.deleteTitle'),
      prompt: t('links.deletePrompt', { name: row.name || row.token }),
      confirmText: t('links.delete'),
      confirmColor: 'danger',
    });
    if (!ok) return;
    try {
      const { ok: done } = await jsonRequest('/api/invites/delete', { tokens: [row.token] });
      if (!done) throw new Error();
      await load();
    } catch {
      error = t('links.deleteFailed');
    }
  }

  async function openDetail(row) {
    detailError = '';
    detail = { row, uploads: null, name: (row.name || '').trim(), exp: dateStr(row.expiresAt) };
    try {
      const { body } = await fetchJson(`/api/invite/${row.token}/uploads`);
      detail.uploads = (body && body.items) || [];
    } catch {
      detail.uploads = [];
    }
  }

  const detailDirty = $derived(!!detail && (detail.name !== (detail.row.name || '').trim() || detail.exp !== dateStr(detail.row.expiresAt)));

  async function saveDetail() {
    const payload = { name: detail.name.trim() };
    // Only send the expiry when it was changed: an unchanged, already expired date would be rejected
    if (detail.exp !== dateStr(detail.row.expiresAt)) {
      if (detail.exp) {
        const dt = new Date(detail.exp);
        dt.setHours(23, 59, 59, 999);
        payload.expiresAt = dt.toISOString().slice(0, 19);
      } else {
        payload.expiresAt = null;
      }
    }
    saving = true;
    detailError = '';
    try {
      const { ok } = await jsonRequest(`/api/invite/${detail.row.token}`, payload, 'PATCH');
      if (!ok) throw new Error();
      detail = null;
      await load();
    } catch {
      detailError = t('links.saveFailed');
    } finally {
      saving = false;
    }
  }
</script>

<Card>
  <CardHeader>
    <CardTitle>{t('home.links.title')}</CardTitle>
    <Text size="small" color="muted" class="mt-2">{t('home.links.desc')}</Text>
  </CardHeader>
  <CardBody>
    <div class="mb-3 flex flex-wrap gap-2">
      <Button class="ps-[16.7px]" leadingIcon={mdiPlus} onclick={onNew}>{t('links.new')}</Button>
      <div class="min-w-48 flex-1"><Input placeholder={t('links.search')} bind:value={q} /></div>
      <div class="w-44"><Select options={sorts} bind:value={sort} /></div>
    </div>
    {#if error}<Text size="small" color="danger" class="mb-2">{error}</Text>{/if}
    <div class="overflow-x-auto">
      <Table striped size="small">
        <TableHeader>
          <TableRow>
            <TableHeading class="text-start">{t('links.col.name')}</TableHeading>
            <TableHeading>{t('links.col.status')}</TableHeading>
            <TableHeading>{t('links.col.uses')}</TableHeading>
            <TableHeading>{t('links.col.expires')}</TableHeading>
            <TableHeading>{t('links.col.album')}</TableHeading>
            <TableHeading></TableHeading>
          </TableRow>
        </TableHeader>
        <TableBody>
          {#each rows as row (row.token)}
            {@const st = statusBadge(row)}
            <TableRow class={flashToken === row.token ? 'bg-success-100 dark:bg-success-900' : ''}>
              <TableCell class="ps-4 text-start font-medium">
                <div class="flex items-center gap-1.5">
                  <button type="button" class="min-w-0 cursor-pointer truncate text-start hover:underline" title={row.name} onclick={() => openDetail(row)}>
                    {row.name || row.token.slice(0, 8)}
                  </button>
                  {#if row.passwordRequired}
                    <Tooltip text={t('links.protected')}>
                      {#snippet child({ props })}
                        <span {...props} role="img" aria-label={t('links.protected')} class="inline-flex shrink-0 text-gray-500 dark:text-gray-400">
                          <Icon icon={mdiLock} size="14" />
                        </span>
                      {/snippet}
                    </Tooltip>
                  {/if}
                </div>
              </TableCell>
              <TableCell><Badge color={st.color}>{st.label}</Badge></TableCell>
              <TableCell>{row.used || 0}/{row.maxUses < 0 ? '∞' : row.maxUses}</TableCell>
              <TableCell>{row.expiresAt ? new Date(row.expiresAt).toLocaleDateString(locale()) : t('links.never')}</TableCell>
              <TableCell>{row.albumName || '–'}</TableCell>
              <TableCell>
                {@const acts = rowActions(row)}
                <div class="flex w-full items-center justify-end gap-1" bind:clientWidth={actionsW}>
                  {#each acts.slice(0, inlineCount) as a (a.label)}
                    <IconButton icon={a.icon} aria-label={a.label} size="small" variant="ghost" color={a.color ?? 'primary'} class="shrink-0" onclick={a.run} />
                  {/each}
                  {#if inlineCount < ACTION_COUNT}
                    <ContextMenuButton aria-label={t('links.more')} size="small" color="primary" class="shrink-0" items={acts.slice(inlineCount).map(toMenuItem)} />
                  {/if}
                </div>
              </TableCell>
            </TableRow>
          {:else}
            <TableRow>
              <TableCell colspan={6}>
                <Text color="muted" class="py-4 text-center">{loaded ? t('links.empty') : t('links.loading')}</Text>
              </TableCell>
            </TableRow>
          {/each}
        </TableBody>
      </Table>
    </div>
  </CardBody>
</Card>

{#if qrUrl}
  <Dialog title={t('links.qr')} size="small" onClose={() => (qrUrl = '')}>
    <LinkShare url={qrUrl} />
  </Dialog>
{/if}

{#if detail}
  <Dialog title={t('links.detailTitle')} size="large" onClose={() => (detail = null)}>
    <div class="flex flex-col gap-4">
      <div class="grid gap-3 sm:grid-cols-2">
        <Field label={t('links.field.name')}><Input bind:value={detail.name} /></Field>
        <Field label={t('links.field.expires')}><Input type="date" min={today} max={maxDate} bind:value={detail.exp} /></Field>
      </div>
      {#if detail.row.passwordRequired}
        <div class="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-300">
          <Icon icon={mdiLock} size="16" />
          {t('links.protected')}
        </div>
      {/if}
      {#if detailError}<Text size="small" color="danger">{detailError}</Text>{/if}
      <div><Button loading={saving} disabled={!detailDirty} onclick={saveDetail}>{t('links.saveChanges')}</Button></div>

      <Text fontWeight="semi-bold">{t('links.uploads')}</Text>
      {#if detail.uploads === null}
        <Text color="muted">{t('links.loading')}</Text>
      {:else if detail.uploads.length}
        <div class="max-h-[40vh] overflow-auto">
          <Table size="small" striped>
            <TableHeader>
              <TableRow>
                <TableHeading>{t('links.col.when')}</TableHeading>
                <TableHeading>{t('links.col.ip')}</TableHeading>
                <TableHeading>{t('links.col.file')}</TableHeading>
                <TableHeading>{t('links.col.size')}</TableHeading>
              </TableRow>
            </TableHeader>
            <TableBody>
              {#each detail.uploads as it}
                <TableRow>
                  <TableCell>{new Date(it.uploadedAt).toLocaleString(locale())}</TableCell>
                  <TableCell>{it.ip}</TableCell>
                  <TableCell class="break-all">{it.filename}</TableCell>
                  <TableCell>{formatBytes(it.size || 0)}</TableCell>
                </TableRow>
              {/each}
            </TableBody>
          </Table>
        </div>
      {:else}
        <Text color="muted">{t('links.noUploads')}</Text>
      {/if}
    </div>
  </Dialog>
{/if}
