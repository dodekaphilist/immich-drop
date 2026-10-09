<script>
  import { onMount } from 'svelte';
  import { Badge, Button, Card, CardBody, CardHeader, CardTitle, Icon, Text, Textarea, Tooltip } from '@immich/ui';
  import { mdiCookieOutline, mdiInformationOutline } from '@mdi/js';
  import { config, connection, fetchJson, jsonRequest } from './config.svelte.js';
  import { t } from './i18n.svelte.js';
  import { u } from './paths.js';
  import AlbumPicker from './AlbumPicker.svelte';
  import CookiesModal from './CookiesModal.svelte';

  let input = $state('');
  let platforms = $state([]);
  let jobs = $state([]); // { id, url, state, msgKey, msgParams, raw, filename, platform }
  let cookiesOpen = $state(false);
  let album = $state({ id: '', name: '' });
  let hint = $state(false);
  let hintTimer;

  // Wait for a confirmed connection: a download can only fail while Immich is unreachable (or not yet known to be reachable).
  // Without the connection check there is nothing to wait for.
  const blocked = $derived(config.test_connection_enabled && connection.status !== 'ok');
  const blockedText = $derived(connection.status === 'down' ? t('status.unreachable') : t('status.checking'));

  function showHint() {
    hint = true;
    clearTimeout(hintTimer);
    hintTimer = setTimeout(() => (hint = false), 3000); // touch has no hover-out to close it
  }

  const splitUrls = (s) => s.split(/[\n\s]+/).map((u) => u.trim()).filter(Boolean);
  const urlCount = $derived(splitUrls(input).length);

  onMount(async () => {
    try {
      const { body } = await fetchJson('/api/supported-platforms');
      platforms = body.platforms || [];
    } catch (e) {
      console.warn('Could not load supported platforms:', e);
    }
  });

  async function pollJob(jobId, onState, intervalMs = 3000, maxAttempts = 100) {
    for (let i = 0; i < maxAttempts; i++) {
      await new Promise((r) => setTimeout(r, intervalMs));
      try {
        const resp = await fetch(u(`/api/upload/url/status/${jobId}`));
        if (!resp.ok) {
          if (resp.status === 404) return { status: 'failed', msgKey: 'url.msg.expired' };
          continue;
        }
        const job = await resp.json();
        onState(job.status);
        if (job.status === 'completed' || job.status === 'failed') return job;
      } catch {
        // network error, keep polling
      }
    }
    return { status: 'failed', msgKey: 'url.msg.polling' };
  }

  function fail(job, msgKey, raw = '', msgParams = {}) {
    job.state = 'failed';
    job.msgKey = raw ? '' : msgKey;
    job.msgParams = msgParams;
    job.raw = raw;
  }

  async function run(job, albumName) {
    try {
      // An empty album name means "no album"; the server only falls back to its default when it is omitted.
      const { body: data } = await jsonRequest('/api/upload/url', { url: job.url, album_name: albumName });
      if (data.detail === 'no_api_key') return fail(job, 'url.msg.noKey');
      if (!data.job_id) return fail(job, 'url.msg.failed', data.detail || data.error || '');
      job.state = 'downloading';
      const result = await pollJob(data.job_id, (st) => {
        if (st === 'downloading' || st === 'uploading') job.state = st;
      });
      if (result.status === 'completed' && result.result) {
        const u = result.result;
        const first = u.result || {};
        job.filename = first.filename || '';
        job.platform = first.platform || '';
        if (u.total_uploaded > 1) {
          job.state = 'done';
          job.msgKey = 'url.msg.items';
          job.msgParams = { n: u.total_uploaded };
        } else if (first.duplicate) {
          job.state = 'duplicate';
          job.msgKey = 'url.msg.dup';
        } else {
          job.state = 'done';
          job.msgKey = 'url.msg.ok';
        }
      } else if (result.status === 'failed') {
        fail(job, result.msgKey || 'url.msg.dlFailed', result.error || '');
      } else {
        fail(job, 'url.msg.timeout');
      }
    } catch (e) {
      fail(job, 'url.msg.error', '', { msg: e.message });
    }
  }

  function submit() {
    const urls = splitUrls(input);
    if (!urls.length) return;
    const albumName = album.name;
    input = '';
    for (const url of urls) {
      jobs.unshift({ id: crypto.randomUUID(), url, state: 'queued', msgKey: '', msgParams: {}, raw: '', filename: '', platform: '' });
      run(jobs[0], albumName);
    }
  }

  const COLOR = { queued: 'secondary', downloading: 'primary', uploading: 'primary', done: 'success', duplicate: 'warning', failed: 'danger' };
  const detail = (job) => [job.platform, job.msgKey ? t(job.msgKey, job.msgParams) : job.raw].filter(Boolean).join(' · ');
</script>

<Card>
  <CardHeader>
    <div class="flex items-center gap-2">
      <CardTitle>{t('home.save.title')}</CardTitle>
      {#if platforms.length}
        <Tooltip text={t('url.supportedTip', { list: platforms.join(', ') })}>
          {#snippet child({ props })}
            <span {...props} role="img" aria-label={t('url.supported')} class="inline-flex text-gray-500 dark:text-gray-400">
              <Icon icon={mdiInformationOutline} size="18" />
            </span>
          {/snippet}
        </Tooltip>
      {/if}
    </div>
    <Text size="small" color="muted" class="mt-2">{t('home.save.desc')}</Text>
  </CardHeader>
  <CardBody>
    <div class="flex flex-col gap-4">
      <div class="flex flex-col gap-2">
        <div>
          <Button size="small" variant="outline" class="bg-transparent" leadingIcon={mdiCookieOutline} onclick={() => (cookiesOpen = true)}>{t('url.cookies')}</Button>
        </div>
        <Textarea rows={3} placeholder={t('url.placeholder')} bind:value={input} />
      </div>

      <AlbumPicker bind:value={album} />

      <div>
        <div class="relative inline-block">
          <Button disabled={!urlCount || blocked} onclick={submit}>
            {urlCount > 1 ? t('url.submitN', { n: urlCount }) : t('url.submit')}
          </Button>
          {#if blocked}
            <!-- a disabled button gets no hover or tap, so a transparent cover takes them; a tap also opens the tooltip on touch screens -->
            <Tooltip text={blockedText} delayDuration={0} bind:open={hint}>
              {#snippet child({ props })}
                <div {...props} class="absolute inset-0 cursor-not-allowed" onclick={showHint} role="presentation"></div>
              {/snippet}
            </Tooltip>
          {/if}
        </div>
      </div>

      {#each jobs as job (job.id)}
        <div class="flex items-center justify-between gap-3 rounded-xl border p-3 dark:border-gray-700">
          <div class="flex min-w-0 flex-col">
            <Text class="truncate" title={job.url} fontWeight="medium">{job.filename || job.url}</Text>
            {#if detail(job)}<Text size="small" color="muted">{detail(job)}</Text>{/if}
          </div>
          <Badge color={COLOR[job.state]}>{t('s.' + job.state)}</Badge>
        </div>
      {/each}
    </div>
  </CardBody>
</Card>

{#if cookiesOpen}<CookiesModal onClose={() => (cookiesOpen = false)} />{/if}
