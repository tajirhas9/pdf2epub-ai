import { describe, expect, it } from 'vitest'
import { toChapters } from '../plugins/epub/chapters.ts'

describe('toChapters', () => {
  it('resolves fragment links whose target lands in another chapter', () => {
    const chapters = toChapters(
      [
        { key: 'p1', html: '<h1>One</h1><p>text<a id="ref-n1" href="#n1">1</a><a href="#local">x</a><span id="local"></span></p>' },
        { key: 'p2', html: '<h1>Two</h1><aside id="n1"><a href="#ref-n1">1</a></aside><a href="#missing">m</a>' },
      ],
      'Book',
    )
    expect(chapters.map((c) => c.file)).toEqual(['ch001.xhtml', 'ch002.xhtml'])
    expect(chapters[0]!.body).toContain('href="ch002.xhtml#n1"')
    expect(chapters[0]!.body).toContain('href="#local"')
    expect(chapters[1]!.body).toContain('href="ch001.xhtml#ref-n1"')
    expect(chapters[1]!.body).toContain('href="#missing"')
  })
})
