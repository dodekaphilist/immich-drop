<script>
  import { onMount } from 'svelte';
  import { config, loadAlbums, loadConfig } from '$lib/config.svelte.js';
  import AppShell from '$lib/AppShell.svelte';
  import CreateLinkModal from '$lib/CreateLinkModal.svelte';
  import ManageLinks from '$lib/ManageLinks.svelte';
  import UrlUploader from '$lib/UrlUploader.svelte';

  let creating = $state(false);
  let highlight = $state(null);
  let refreshKey = $state(0);

  function created(token) {
    highlight = token;
    refreshKey++;
  }

  onMount(() => {
    loadConfig();
    loadAlbums(); // warm the album list so the dialogs open with it ready
  });
</script>

<svelte:head><title>Immich Drop</title></svelte:head>

<AppShell owner size="giant">
  <ManageLinks {highlight} {refreshKey} onNew={() => (creating = true)} />
  {#if config.loaded && config.social_media_uploads}
    <UrlUploader />
  {/if}
</AppShell>

{#if creating}<CreateLinkModal onClose={() => (creating = false)} onCreated={created} />{/if}
