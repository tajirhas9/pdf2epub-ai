import type { InjectionKey } from 'vue'

/** Prefix prepended to book-relative asset paths (dev server URL or EPUB-relative path). */
export const ASSET_BASE: InjectionKey<string> = Symbol('assetBase')
