// Shapes the screen-annotation overlay paints (annotate_screen tool).
//
// Electron main is the authority: it resolves the target window and maps the
// agent's frame-pixel coordinates into overlay-local DIP before anything
// reaches this renderer, so these are ready-to-paint values. This mirror of
// electron/screen-annotations.ts' MappedAnnotationShape exists because the
// renderer and main compile in separate TS projects and share only the IPC
// payload shape.

export type ScreenAnnotationColor = 'black' | 'blue' | 'green' | 'orange' | 'purple' | 'red' | 'white' | 'yellow'

export interface ScreenAnnotationShapeBase {
  color: ScreenAnnotationColor
  /** Optional short caption drawn beside the mark. */
  label?: string
  /** Skip the entrance/pulse animation — for shapes that replace each other
   *  rapidly (live subtitles), where pulsing text is unreadable. */
  steady?: boolean
}

export interface ScreenAnnotationCircle extends ScreenAnnotationShapeBase {
  kind: 'circle'
  radius: number
  x: number
  y: number
}

export interface ScreenAnnotationRect extends ScreenAnnotationShapeBase {
  /** Opaque cover instead of an outline (subtitle backdrop). */
  fill?: boolean
  height: number
  kind: 'rect'
  width: number
  x: number
  y: number
}

export interface ScreenAnnotationStroke extends ScreenAnnotationShapeBase {
  fromX: number
  fromY: number
  kind: 'arrow' | 'line'
  toX: number
  toY: number
}

export interface ScreenAnnotationLabel extends ScreenAnnotationShapeBase {
  /** DIP font size; the pointing-caption default when absent. */
  fontSize?: number
  kind: 'label'
  text: string
  x: number
  y: number
}

export type ScreenAnnotationShape =
  ScreenAnnotationCircle | ScreenAnnotationLabel | ScreenAnnotationRect | ScreenAnnotationStroke

/** Vivid, screen-legible strokes (Apple system palette hues). */
export const SCREEN_ANNOTATION_HEX: Record<ScreenAnnotationColor, string> = {
  black: '#111111',
  blue: '#0A84FF',
  green: '#30D158',
  orange: '#FF9F0A',
  purple: '#BF5AF2',
  red: '#FF3B30',
  white: '#FFFFFF',
  yellow: '#FFD60A'
}
