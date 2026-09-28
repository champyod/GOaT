import { svelteTesting } from '@testing-library/svelte/vite';
import type { Plugin, UserConfig } from 'vite';
import { defineConfig } from 'vitest/config';
import viteConfig from './vite.config.ts';

/// The app keeps its SvelteKit configuration inlined in `vite.config.ts`
/// rather than in a `svelte.config.js`, so the test config has to read the
/// plugins from there: a second `sveltekit()` call would compile the same
/// components under two sets of compiler options.
const base = await (viteConfig as UserConfig);

async function resolvePlugins(options: UserConfig['plugins']): Promise<Plugin[]> {
  const resolved = (await Promise.all(options ?? [])).flat();
  return resolved.filter(
    (plugin): plugin is Plugin => plugin !== false && plugin != null
  );
}

export default defineConfig({
  plugins: [...(await resolvePlugins(base.plugins)), svelteTesting()],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts'],
    setupFiles: ['./src/test-setup.ts'],
    /// The stubbed clipboard is a `vi.fn`, so without this a test asserting what
    /// was written to it also passes on the writes of the test before it.
    clearMocks: true
  }
});
