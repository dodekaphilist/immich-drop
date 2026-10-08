<script>
  import '../app.css';
  import { onMount } from 'svelte';
  import { TooltipProvider, themeManager } from '@immich/ui';
  import { fitTooltips } from '$lib/tooltip-fit.js';
  let { children } = $props();

  // The theme manager only touches the DOM on toggle/system change; sync once so the
  // stored preference (default: follow the browser) is applied on load.
  onMount(() => {
    themeManager.setPreference(themeManager.preference);
    return fitTooltips();
  });
</script>

<TooltipProvider>
  {@render children()}
</TooltipProvider>
