<script>
  import { themeManager } from '@immich/ui';
  import { u } from './paths.js';

  /** The Immich Drop icon in the variant that suits the active theme. @type {{ class?: string }} */
  let { class: className = '' } = $props();

  const src = $derived(u(`/static/immich-drop-${themeManager.value === 'dark' ? 'dark' : 'light'}.svg`));
</script>

<img {src} class={className} alt="Immich Drop logo" draggable="false" />
