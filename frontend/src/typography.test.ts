/// <reference types="node" />

import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { darkTheme, lightTheme } from './theme'

const css = readFileSync(new URL('./index.css', import.meta.url), 'utf8')
const expectedFontFamily =
  "TCloudNumber, -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Microsoft YaHei', Arial, sans-serif"

describe('TDesign typography', () => {
  it('bundles the official number font and applies the global font stack', () => {
    expect(css).toContain("font-family: 'TCloudNumber'")
    expect(css).toContain("url('/fonts/TCloudNumberVF.ttf') format('truetype')")
    expect(css).toContain('font-display: swap')
    expect(css).toContain('--font-family-tdesign:')
    expect(css).toMatch(/body\s*{[^}]*font-family:\s*var\(--font-family-tdesign\)/s)
  })

  it('keeps technical content on a monospace stack', () => {
    expect(css).toContain('--font-family-mono:')
    expect(css).toMatch(/pre,\s*code\s*{[^}]*font-family:\s*var\(--font-family-mono\)/s)
  })

  it('uses the same font stack in both Ant Design themes', () => {
    const darkToken = darkTheme.token as { fontFamily?: string }
    const lightToken = lightTheme.token as { fontFamily?: string }

    expect(darkToken.fontFamily).toBe(expectedFontFamily)
    expect(lightToken.fontFamily).toBe(expectedFontFamily)
  })
})
