<script>
  import { onMount } from 'svelte';
  import { Container, IconButton, Text } from '@immich/ui';
  import { mdiLogout } from '@mdi/js';
  import { t } from './i18n.svelte.js';
  import { u } from './paths.js';
  import AppLogo from './AppLogo.svelte';
  import SettingsButton from './SettingsButton.svelte';
  import { refreshKey } from './apikey.svelte.js';

  /** @type {{ owner?: boolean, size?: 'medium'|'large'|'giant', footer?: string, children: import('svelte').Snippet }} */
  let { owner = false, size = 'large', footer = '', children } = $props();

  onMount(() => {
    if (owner) refreshKey();
  });
</script>

<Container {size} center class="px-4 py-6">
  <header class="mb-6 flex flex-wrap items-center justify-between gap-3">
    <div class="flex items-center gap-4">
      <AppLogo class="h-8" />
      <Text fontWeight="bold" class="text-2xl">Immich Drop</Text>
    </div>
    <div class="flex items-center gap-2">
      <SettingsButton {owner} />
      {#if owner}
        <IconButton href={u('/logout')} icon={mdiLogout} shape="round" variant="ghost" size="small" aria-label={t('nav.logout')} />
      {/if}
    </div>
  </header>

  <main class="flex flex-col gap-4">
    {@render children()}
  </main>

  {#if footer}
    <footer class="mt-8 text-center"><Text size="small" color="muted">{footer}</Text></footer>
  {/if}
</Container>
