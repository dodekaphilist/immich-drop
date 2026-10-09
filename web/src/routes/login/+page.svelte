<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Card, CardBody, CardHeader, CardTitle, Field, Input, PasswordInput, Stack } from '@immich/ui';
  import { t } from '$lib/i18n.svelte.js';
  import { u } from '$lib/paths.js';
  import AppLogo from '$lib/AppLogo.svelte';
  import ConnectionIndicator from '$lib/ConnectionIndicator.svelte';
  import SettingsButton from '$lib/SettingsButton.svelte';

  let email = $state('');
  let password = $state('');
  let busy = $state(false);
  let errorKey = $state('');
  let localLogin = $state(null); // unknown until /api/auth/options answers, so the form never flashes
  let oauth = $state(false);
  let ssoText = $state('');
  let message = $state('');

  onMount(async () => {
    const params = new URLSearchParams(location.search);
    const err = params.get('error');
    if (err === 'oauth_failed') errorKey = 'login.ssoFailed';
    else if (err === 'oauth_disabled') errorKey = 'login.ssoDisabled';
    let o = { password: true, oauth: false, autoLaunch: false };
    try {
      const r = await fetch(u('/api/auth/options'), { headers: { Accept: 'application/json' } });
      if (r.ok) o = await r.json();
    } catch {
      /* fall back to password only */
    }
    if (o.oauth && o.autoLaunch && !err && params.get('autoLaunch') !== '0') {
      location.replace(u('/oauth/start'));
      return;
    }
    localLogin = !!o.password;
    oauth = !!o.oauth;
    ssoText = o.oauthButtonText || '';
    message = o.message || '';
    if (!o.password && !o.oauth) errorKey = 'login.noMethod';
  });

  async function submit(event) {
    event.preventDefault();
    errorKey = '';
    busy = true;
    try {
      const r = await fetch(u('/api/login'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ email: email.trim(), password }),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) {
        errorKey = j.error === 'unauthorized' ? 'login.badCredentials' : 'login.failed';
        return;
      }
      location.href = u('/');
    } catch {
      errorKey = 'login.failed';
    } finally {
      busy = false;
    }
  }
</script>

<svelte:head><title>{t('login.submit')} - Immich Drop</title></svelte:head>

<main class="flex min-h-screen items-center justify-center p-4">
  <div class="w-full max-w-md">
    <div class="mb-6 flex items-center justify-between">
      <AppLogo class="h-12" />
      <div class="flex items-center gap-2">
        <ConnectionIndicator />
        <SettingsButton />
      </div>
    </div>

    <Card>
      <CardHeader>
        <CardTitle>{t('login.title')}</CardTitle>
      </CardHeader>
      <CardBody>
        <Stack gap={4}>
          {#if message}<p class="text-sm">{message}</p>{/if}
          {#if errorKey}<Alert color="danger" title={t(errorKey)} />{/if}

          {#if localLogin === true}
            <form onsubmit={submit}>
              <Stack gap={4}>
                <Field label={t('login.email')} required>
                  <Input type="email" autocomplete="email" bind:value={email} />
                </Field>
                <Field label={t('login.password')} required>
                  <PasswordInput autocomplete="current-password" bind:value={password} />
                </Field>
                <Button type="submit" fullWidth loading={busy}>{t('login.submit')}</Button>
              </Stack>
            </form>
          {/if}
          {#if oauth}
            <Button href={u('/oauth/start')} variant={localLogin ? 'outline' : 'filled'} fullWidth>{ssoText || t('login.sso')}</Button>
          {/if}
        </Stack>
      </CardBody>
    </Card>
  </div>
</main>
