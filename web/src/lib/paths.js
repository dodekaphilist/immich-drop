import { base } from '$app/paths';

// The app can run under a subfolder (e.g. https://immich.example.com/drop). The server injects that
// path into the page at runtime, so `base` is "" at the root and "/drop" under a subfolder.

/** Prefix an absolute app path ("/api/...", "/oauth/start") with the subfolder. */
export const u = (path) => base + path;
