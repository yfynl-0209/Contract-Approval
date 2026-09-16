import type { ReactNode } from 'react'
import { useMemo } from 'react'
import { Document, Page, pdfjs } from 'react-pdf'
import workerSrc from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
// ⚠️ 借一个已知文件名反推出**目录 URL**：cMaps / 标准字体是几百个小文件，
// 逐个 import 不现实；pdf.js 只要拿到目录前缀就会自己拼文件名去取。
// （没有它们时，非内嵌字体的页面渲染会因字体资源缺失而中止——画布一片空白。）
import someCMap from 'pdfjs-dist/cmaps/Adobe-CNS1-0.bcmap?url'
import someFont from 'pdfjs-dist/standard_fonts/FoxitSerif.pfb?url'

import type { DocumentPage } from '../../api/contracts'
import { pageRotationForRender, type Viewport } from './coordinateTransform'

function directoryOf(fileUrl: string): string {
  return fileUrl.slice(0, fileUrl.lastIndexOf('/') + 1)
}
const CMAP_URL = directoryOf(someCMap)
const STANDARD_FONTS_URL = directoryOf(someFont)

/**
 * Document 的 options **必须是稳定引用**：react-pdf 按引用比较 options，
 * 裸对象字面量每次渲染都是"新配置"，会触发整个文档的重新初始化。
 */
const DOCUMENT_OPTIONS = {
  disableAutoFetch: true,
  disableStream: true,
  cMapUrl: CMAP_URL,
  cMapPacked: true,
  standardFontDataUrl: STANDARD_FONTS_URL,
} as const

/**
 * PDF 单页渲染 + 证据层（M8 Task 5）。
 *
 * ## 字节由**我们自己**取（入参是 `ArrayBuffer`，不是 URL）
 *
 * 让渲染库自己去取 URL 会丢掉两样东西：我们的身份头，以及后端的
 * `error_code`（附件取不到有 `RESOURCE_NOT_FOUND` 与 `OBJECT_NOT_FOUND`
 * 两种原因，处置相反 —— 见 `client.ts::requestBytes`）。
 *
 * ## 旋转**只用文档的值**（唯一一处）
 *
 * `rotate={pageRotationForRender(page)}` 取的是 `DocumentPage.rotation`，
 * 而不是 PDF 自己的 `/Rotate`。理由：几何框已在**文档定义的**可见页空间里，
 * 渲染必须与那个空间一致；取 PDF 的元数据时，两份旋转信息不一致的页
 * 会渲染成另一个方向，框就整体错位（见 `coordinateTransform.ts` 文件头）。
 *
 * ## ⚠️ 这个组件**没有单元测试**，这是有意的
 *
 * 它把字节交给 pdf.js 在 `<canvas>` 上渲染，而 jsdom **没有 canvas 实现**
 * （`canvas` 包在安装时被 `--ignore-scripts` 跳过了，浏览器端也不需要它）。
 * 与其写一个"渲染了个空 canvas 也算通过"的假测试，不如：
 *
 * 1. 把**能测的部分**（几何变换、证据框构造、字段四态、降级路径）全部抽成纯函数
 *    并测到位（`coordinateTransform.test.ts` / `evidenceBoxes` / `ParseTab.test.tsx`）；
 * 2. 在这个组件里只留**无法在 jsdom 里验证**的粘合代码，并把它压到最短。
 *
 * 这是"没测到"的如实记录，不是"测过了"。
 */
export function PdfPageCanvas({
  data,
  page,
  viewport,
  children,
}: {
  readonly data: ArrayBuffer
  readonly page: DocumentPage
  readonly viewport: Viewport
  /** 证据层（由调用方算好坐标后传入 —— 这一层不做几何） */
  readonly children?: ReactNode
}): JSX.Element {
  /*
   * ⚠️ 两件必须做的事，都是 M8 演示现场第一次真浏览器渲染时暴露的：
   *
   * 1. **交给 pdf.js 的是拷贝**（`data.slice(0)`）：pdf.js 会把 ArrayBuffer
   *    **转移**给 worker（postMessage transfer），原缓冲区随即"卸下"
   *    （detached）—— 再拿它加载就是 `TypeError: detached ArrayBuffer`，
   *    表现为"这份 PDF 无法渲染（文件可能损坏）"，而文件其实是好的。
   * 2. **file / options 必须是稳定引用**：react-pdf 按引用比较，
   *    裸对象字面量每次渲染都被当成"换了个文件"，触发重新解析。
   */
  const file = useMemo(() => ({ data: data.slice(0) }), [data])
  return (
    <div
      data-testid="pdf-page"
      style={{
        position: 'relative',
        width: `${viewport.width}px`,
        height: `${viewport.height}px`,
        background: '#fff',
      }}
    >
      <Document
        file={file}
        // 关掉 PDF 自带的注释/文本层：文本层会带来它自己的 DOM 与选择行为，
        // 与我们的证据层叠加时"选中文字"和"点证据框"会互相抢事件
        options={DOCUMENT_OPTIONS}
        loading={<span>正在渲染…</span>}
        error={<span>这份 PDF 无法渲染（文件可能损坏）</span>}
      >
        <Page
          pageNumber={page.page}
          width={viewport.width}
          rotate={pageRotationForRender(page)}
          renderAnnotationLayer={false}
          renderTextLayer={false}
        />
      </Document>
      {children}
    </div>
  )
}

/** pdf.js 的 worker：**必须**指定，否则主线程渲染并在控制台刷警告。 */
pdfjs.GlobalWorkerOptions.workerSrc = workerSrc
