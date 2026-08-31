import { describe, expect, it } from 'vitest'

import { resolveIntroCopy } from './intro'

describe('intro copy localization', () => {
  it('uses Brazilian Portuguese copy for neutral intros', () => {
    expect(resolveIntroCopy('none', 3, 'pt-br')).toEqual({
      headline: 'Pronto quando você estiver.',
      body: 'Digite uma tarefa, pergunta ou trecho. Eu lembro da sessão, cito minhas fontes e paro para perguntar quando não tenho certeza.'
    })
  })

  it('keeps custom-personality fallback copy in Brazilian Portuguese', () => {
    const copy = resolveIntroCopy('pirate', 0, 'pt-br')

    expect(copy.headline).toBe('O modo Pirate está ativo. Em que vamos trabalhar?')
    expect(copy.body).toContain('Envie a tarefa')
  })
})
