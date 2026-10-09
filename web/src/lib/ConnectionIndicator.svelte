<script>
  import { onMount } from 'svelte';
  import { Tooltip } from '@immich/ui';
  import { config, connection, watchConnection } from './config.svelte.js';
  import { t } from './i18n.svelte.js';

  const text = $derived(
    connection.status === 'unknown'
      ? t('status.checking')
      : connection.status === 'down'
        ? t('status.unreachable')
        : connection.host
          ? t('status.connectedAt', { host: connection.host })
          : t('status.connected'),
  );
  const color = $derived(
    { unknown: 'bg-gray-400 animate-pulse', ok: 'bg-success-500', down: 'bg-danger-500' }[connection.status],
  );

  onMount(watchConnection);
</script>

<!-- shown from the first paint (neutral while checking); hidden only once the config says the check is off -->
{#if !config.loaded || config.test_connection_enabled}
  <Tooltip {text} delayDuration={0}>
    {#snippet child({ props })}
      <span {...props} role="img" aria-label={text} class="inline-block size-3 cursor-default rounded-full {color}"></span>
    {/snippet}
  </Tooltip>
{/if}
