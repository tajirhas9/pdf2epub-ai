export interface BBox {
  x0: number
  y0: number
  x1: number
  y1: number
}

export interface BlockMeta {
  id: string
  type: string
  role: string
  bbox: BBox
  text: string
  source: string
  confidence: number
  flags: string[]
  order: number
}

export interface PageMeta {
  key: string
  index: number
  label: string
  kind: string
  image: { path: string; width: number; height: number } | null
  locked: boolean
  flags: string[]
  timings: Record<string, number>
  textLayerQuality: number | null
  blocks: BlockMeta[]
}

export interface BookState {
  book: { id: string; title: string; language: string } | null
  progress: { stage: string; done: number; total: number; t: number } | null
  pages: PageMeta[]
}
