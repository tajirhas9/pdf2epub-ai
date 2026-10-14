import type { App } from 'vue'
import BookFigure from './BookFigure.vue'
import BookGlyph from './BookGlyph.vue'
import BookHeading from './BookHeading.vue'
import BookPara from './BookPara.vue'
import Footnote from './Footnote.vue'
import Lang from './Lang.vue'
import NoteRef from './NoteRef.vue'
import PageBreak from './PageBreak.vue'

export { ASSET_BASE } from './keys'
export { BookFigure, BookGlyph, BookHeading, BookPara, Footnote, Lang, NoteRef, PageBreak }

/** Registers book components globally so generated pages need no imports. */
export function installBook(app: App): App {
  return app
    .component('BookFigure', BookFigure)
    .component('BookGlyph', BookGlyph)
    .component('BookHeading', BookHeading)
    .component('BookPara', BookPara)
    .component('Footnote', Footnote)
    .component('Lang', Lang)
    .component('NoteRef', NoteRef)
    .component('PageBreak', PageBreak)
}
