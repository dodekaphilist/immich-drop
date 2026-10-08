<script>
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { Alert, Badge, Button, Field, Heading, PasswordInput, Text } from '@immich/ui';
  import { fetchJson, jsonRequest } from '$lib/config.svelte.js';
  import { locale, t } from '$lib/i18n.svelte.js';
  import AppShell from '$lib/AppShell.svelte';
  import Uploader from '$lib/Uploader.svelte';

  const token = page.params.token;

  let info = $state(null);
  let loaded = $state(false);
  let password = $state('');
  let pwError = $state('');
  let unlocking = $state(false);
  let authorized = $state(false);

  const locked = $derived(!!info && info.passwordRequired && !authorized && !info.authorized);
  const inactive = $derived(!!info && !info.active);

  // The server reports why a link is inactive as a plain word (expired, claimed, ...)
  const inactiveText = $derived.by(() => {
    const key = `invite.reason.${String(info?.inactiveReason || '').toLowerCase()}`;
    const text = t(key);
    return text === key ? t('invite.inactive') : text;
  });

  const usesLeft = $derived.by(() => {
    if (!info) return '';
    if (info.oneTime) return t('invite.once');
    if (typeof info.remaining !== 'number') return t('invite.unlimited');
    return info.remaining === 1 ? t('invite.left.one') : t('invite.left.other', { n: info.remaining });
  });

  onMount(async () => {
    try {
      const { ok, body } = await fetchJson(`/api/invite/${token}`);
      info = ok ? body : null;
    } catch {}
    loaded = true;
  });

  async function unlock() {
    pwError = '';
    const pw = password.trim();
    if (!pw) {
      pwError = t('invite.pwEmpty');
      return;
    }
    unlocking = true;
    try {
      const { ok, body } = await jsonRequest(`/api/invite/${token}/auth`, { password: pw });
      if (!ok || !body.authorized) {
        pwError = t('invite.pwBad');
        return;
      }
      authorized = true;
    } catch {
      pwError = t('invite.pwError');
    } finally {
      unlocking = false;
    }
  }
</script>

<svelte:head>
  <title>Immich Drop</title>
  <meta name="robots" content="noindex, nofollow" />
</svelte:head>

{#snippet notice(text, color)}
  <div class="mx-auto mt-10 flex w-full max-w-sm flex-col gap-5 text-center">
    <Heading tag="h1" size="large">{t('invite.unavailable')}</Heading>
    <Alert {color} title={text} />
  </div>
{/snippet}

<AppShell>
  {#if loaded && !info}
    {@render notice(t('invite.loadFail'), 'danger')}
  {:else if info}
    {#if inactive}
      <!-- No link name here: an unusable link should not reveal anything about itself. -->
      {@render notice(inactiveText, 'warning')}
    {:else if locked}
      <!-- Only the link name is shown until the password was entered. -->
      <form class="mx-auto mt-10 flex w-full max-w-sm flex-col gap-6" onsubmit={(e) => { e.preventDefault(); unlock(); }}>
        <div class="flex flex-col gap-2 text-center">
          <Heading tag="h1" size="large">{info.name || t('invite.default')}</Heading>
          <Text color="muted">{t('invite.gateText')}</Text>
        </div>
        <div class="flex flex-col gap-3">
          <div class="flex flex-col gap-1.5">
            <Field invalid={!!pwError}>
              <PasswordInput placeholder={t('invite.password')} bind:value={password} autocomplete="new-password" data-1p-ignore data-lpignore="true" data-bwignore data-form-type="other" oninput={() => (pwError = '')} />
            </Field>
            {#if pwError}<Text size="small" color="danger" class="px-1">{pwError}</Text>{/if}
          </div>
          <Button type="submit" fullWidth loading={unlocking}>{t('invite.unlock')}</Button>
        </div>
      </form>
    {:else}
      <Heading tag="h1" size="large">{info.name || t('invite.default')}</Heading>
      <div class="flex flex-wrap gap-2">
        <Badge color="secondary">{usesLeft}</Badge>
        <Badge color="secondary">
          {info.expiresAt ? t('invite.until', { date: new Date(info.expiresAt).toLocaleDateString(locale()) }) : t('invite.noExpiry')}
        </Badge>
      </div>
      <Uploader inviteToken={token} />
    {/if}
  {/if}
</AppShell>
