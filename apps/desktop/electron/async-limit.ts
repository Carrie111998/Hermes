export type AsyncLimiter = <T>(task: () => Promise<T> | T) => Promise<T>

interface PendingTask {
  next: PendingTask | null
  run: () => void
}

/** Create a FIFO limiter shared by every caller that receives this function. */
export function createAsyncLimiter(maxConcurrency: number): AsyncLimiter {
  if (!Number.isInteger(maxConcurrency) || maxConcurrency < 1) {
    throw new Error('maxConcurrency must be a positive integer')
  }

  let active = 0
  let pendingHead: PendingTask | null = null
  let pendingTail: PendingTask | null = null

  const enqueue = (run: () => void) => {
    const pending = { next: null, run }

    if (pendingTail) {
      pendingTail.next = pending
    } else {
      pendingHead = pending
    }

    pendingTail = pending
  }

  const dequeue = () => {
    const pending = pendingHead

    if (!pending) {
      return
    }

    pendingHead = pending.next

    if (!pendingHead) {
      pendingTail = null
    }

    pending.run()
  }

  return task =>
    new Promise((resolve, reject) => {
      const run = () => {
        active += 1
        Promise.resolve()
          .then(task)
          .then(resolve, reject)
          .finally(() => {
            active -= 1
            dequeue()
          })
      }

      if (active < maxConcurrency) {
        run()
      } else {
        enqueue(run)
      }
    })
}
