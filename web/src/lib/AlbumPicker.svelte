<script>
  import { onMount } from 'svelte';
  import { Field, Input, Select } from '@immich/ui';
  import { albumStore, config, loadAlbums, loadConfig } from './config.svelte.js';
  import { t } from './i18n.svelte.js';

  /**
   * Shared album choice for "download from service" and "upload link".
   * `value` is { id, name }: both empty = no album, id + name = existing album, name only = new album.
   * The server's default album (IMMICH_ALBUM_NAME) is preselected when there is one.
   * The album list comes from a shared store that is usually warm already, so the picker opens in its final state.
   * @type {{ value?: { id: string, name: string } }}
   */
  let { value = $bindable({ id: '', name: '' }) } = $props();

  const NONE = '__none__';

  let selected = $state(NONE);
  let typed = $state('');
  let defaultApplied = false; // the default is applied once, and never over a choice the user made

  const ready = $derived(config.loaded && albumStore.loaded);
  const options = $derived(
    albumStore.loaded && !albumStore.ok
      ? [{ label: t('album.unavailable'), value: NONE }]
      : albumStore.loaded
      ? [{ label: t('album.none'), value: NONE }, ...albumStore.items.map((a) => ({ label: a.name, value: a.id }))]
      : [{ label: t('album.loading'), value: NONE }],
  );

  function applyDefault() {
    if (defaultApplied || !ready) return;
    defaultApplied = true;
    const def = config.default_album;
    if (!def) return;
    const match = albumStore.items.find((a) => a.name === def);
    if (match) selected = match.id;
    else typed = def; // does not exist yet: it will be created on first use
    publish();
  }

  function publish() {
    if (typed.trim()) value = { id: '', name: typed.trim() };
    else if (selected !== NONE) value = { id: selected, name: albumStore.items.find((a) => a.id === selected)?.name ?? '' };
    else value = { id: '', name: '' };
  }

  applyDefault(); // synchronously when the data is already there: no flash of the wrong state

  onMount(() => {
    loadConfig();
    loadAlbums(); // no-op while the list is fresh; otherwise a silent refresh
  });

  $effect(applyDefault); // data that arrives after the dialog opened

  $effect(() => {
    // the chosen album was deleted on the server in the meantime
    if (albumStore.ok && selected !== NONE && !albumStore.items.some((a) => a.id === selected)) {
      selected = NONE;
      publish();
    }
  });

  function pick(id) {
    defaultApplied = true;
    selected = id;
    typed = ''; // one choice at a time
    publish();
  }

  function type(event) {
    defaultApplied = true;
    typed = event.currentTarget.value;
    if (typed.trim()) selected = NONE;
    publish();
  }
</script>

<div class="grid gap-3 sm:grid-cols-2">
  <Field label={t('album.label')} disabled={!albumStore.ok}>
    <Select {options} value={selected} onChange={pick} />
  </Field>
  <Field label={t('album.new')}>
    <Input placeholder={t('album.newPlaceholder')} value={typed} oninput={type} />
  </Field>
</div>
