import { useEffect, useState } from 'react'

import {
  SCREEN_ANNOTATION_HEX,
  type ScreenAnnotationColor,
  type ScreenAnnotationShape,
  type ScreenAnnotationStroke
} from './shapes'

// Stroke geometry, in DIP. Every colored stroke rides on a wider contrast halo
// so a mark stays legible over arbitrary content (a white circle on a white
// board, a black arrow over a dark game).
const STROKE_WIDTH = 4
const HALO_WIDTH = 8
const ARROW_HEAD_LENGTH = 16
const LABEL_FONT_SIZE = 15
const LABEL_OFFSET = 10

const hexFor = (color: ScreenAnnotationColor): string => SCREEN_ANNOTATION_HEX[color] ?? SCREEN_ANNOTATION_HEX.red

const haloFor = (color: ScreenAnnotationColor): string =>
  color === 'black' ? 'rgba(255, 255, 255, 0.85)' : 'rgba(0, 0, 0, 0.55)'

/** Caption text with a paint-order halo so it reads over any background. */
function Caption({ color, text, x, y }: { color: ScreenAnnotationColor; text?: string; x: number; y: number }) {
  if (!text) {
    return null
  }

  return (
    <text
      fill={hexFor(color)}
      fontFamily="system-ui, -apple-system, sans-serif"
      fontSize={LABEL_FONT_SIZE}
      fontWeight={700}
      paintOrder="stroke"
      stroke={haloFor(color)}
      strokeWidth={4}
      textAnchor="middle"
      x={x}
      y={y}
    >
      {text}
    </text>
  )
}

/** The line/arrow shaft plus, for arrows, a filled head at the `to` end. The
 *  head is drawn manually (not an SVG marker) so it can carry the same halo. */
function StrokeShape({ shape }: { shape: ScreenAnnotationStroke }) {
  const hex = hexFor(shape.color)
  const halo = haloFor(shape.color)
  const angle = Math.atan2(shape.toY - shape.fromY, shape.toX - shape.fromX)
  const hasHead = shape.kind === 'arrow'

  // Shorten the shaft so it does not poke through the head's tip.
  const shaftToX = hasHead ? shape.toX - Math.cos(angle) * (ARROW_HEAD_LENGTH * 0.6) : shape.toX
  const shaftToY = hasHead ? shape.toY - Math.sin(angle) * (ARROW_HEAD_LENGTH * 0.6) : shape.toY

  const spread = Math.PI / 7

  const headPoints = hasHead
    ? [
        `${shape.toX},${shape.toY}`,
        `${shape.toX - Math.cos(angle - spread) * ARROW_HEAD_LENGTH},${shape.toY - Math.sin(angle - spread) * ARROW_HEAD_LENGTH}`,
        `${shape.toX - Math.cos(angle + spread) * ARROW_HEAD_LENGTH},${shape.toY - Math.sin(angle + spread) * ARROW_HEAD_LENGTH}`
      ].join(' ')
    : ''

  return (
    <g>
      <line
        fill="none"
        stroke={halo}
        strokeLinecap="round"
        strokeWidth={HALO_WIDTH}
        x1={shape.fromX}
        x2={shaftToX}
        y1={shape.fromY}
        y2={shaftToY}
      />
      {hasHead ? <polygon fill={halo} points={headPoints} stroke={halo} strokeWidth={3} /> : null}
      <line
        fill="none"
        stroke={hex}
        strokeLinecap="round"
        strokeWidth={STROKE_WIDTH}
        x1={shape.fromX}
        x2={shaftToX}
        y1={shape.fromY}
        y2={shaftToY}
      />
      {hasHead ? <polygon fill={hex} points={headPoints} /> : null}
      <Caption
        color={shape.color}
        text={shape.label}
        x={(shape.fromX + shape.toX) / 2}
        y={(shape.fromY + shape.toY) / 2 - LABEL_OFFSET}
      />
    </g>
  )
}

function Shape({ shape }: { shape: ScreenAnnotationShape }) {
  if (shape.kind === 'circle') {
    return (
      <g>
        <circle
          cx={shape.x}
          cy={shape.y}
          fill="none"
          r={shape.radius}
          stroke={haloFor(shape.color)}
          strokeWidth={HALO_WIDTH}
        />
        <circle
          cx={shape.x}
          cy={shape.y}
          fill="none"
          r={shape.radius}
          stroke={hexFor(shape.color)}
          strokeWidth={STROKE_WIDTH}
        />
        <Caption color={shape.color} text={shape.label} x={shape.x} y={shape.y + shape.radius + LABEL_FONT_SIZE + 4} />
      </g>
    )
  }

  if (shape.kind === 'rect') {
    return (
      <g>
        <rect
          fill="none"
          height={shape.height}
          rx={6}
          stroke={haloFor(shape.color)}
          strokeWidth={HALO_WIDTH}
          width={shape.width}
          x={shape.x}
          y={shape.y}
        />
        <rect
          fill="none"
          height={shape.height}
          rx={6}
          stroke={hexFor(shape.color)}
          strokeWidth={STROKE_WIDTH}
          width={shape.width}
          x={shape.x}
          y={shape.y}
        />
        <Caption color={shape.color} text={shape.label} x={shape.x + shape.width / 2} y={shape.y - LABEL_OFFSET} />
      </g>
    )
  }

  if (shape.kind === 'arrow' || shape.kind === 'line') {
    return <StrokeShape shape={shape} />
  }

  if (shape.kind === 'label') {
    return <Caption color={shape.color} text={shape.text} x={shape.x} y={shape.y} />
  }

  return null
}

/**
 * The screen-annotation overlay (annotate_screen tool). A full-window SVG in a
 * transparent, click-through OS window; main pushes ready-to-paint shapes and
 * this component only renders them. Pull-then-subscribe: the push that
 * announced these shapes can predate this lazy chunk's mount.
 */
export function ScreenAnnotationsApp() {
  const [shapes, setShapes] = useState<ScreenAnnotationShape[]>([])

  useEffect(() => {
    const bridge = window.hermesDesktop?.screenAnnotations

    if (!bridge) {
      return
    }

    let disposed = false

    bridge
      .getState()
      .then(state => {
        if (!disposed && state && Array.isArray(state.shapes)) {
          setShapes(state.shapes)
        }
      })
      .catch(() => {
        // An older shell without the handler — pushes still work.
      })

    const unsubscribe = bridge.onState(payload => {
      setShapes(payload && Array.isArray(payload.shapes) ? payload.shapes : [])
    })

    return () => {
      disposed = true
      unsubscribe()
    }
  }, [])

  return (
    <>
      <style>
        {`
          @keyframes hermes-annotation-in {
            from { opacity: 0; transform: scale(1.04); }
            to { opacity: 1; transform: scale(1); }
          }
          @keyframes hermes-annotation-pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.6; }
          }
          .hermes-annotation {
            animation: hermes-annotation-in 180ms ease-out, hermes-annotation-pulse 1.8s ease-in-out 180ms infinite;
            transform-origin: center;
          }
        `}
      </style>
      <svg style={{ height: '100vh', left: 0, position: 'fixed', top: 0, width: '100vw' }}>
        {shapes.map((shape, index) => (
          <g className="hermes-annotation" key={index}>
            <Shape shape={shape} />
          </g>
        ))}
      </svg>
    </>
  )
}
