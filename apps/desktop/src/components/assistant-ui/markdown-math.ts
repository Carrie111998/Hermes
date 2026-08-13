import 'katex/dist/katex.min.css'

import { createMemoizedMathPlugin } from '@/lib/katex-memo'

export const mathPlugin = createMemoizedMathPlugin({ singleDollarTextMath: true })
