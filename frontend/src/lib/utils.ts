import {clsx, type ClassValue} from 'clsx'
import {twMerge} from 'tailwind-merge'
import {toast} from 'sonner'
export function cn(...inputs: ClassValue[]) {return twMerge(clsx(inputs))}

/** Copy a value, with a toast either way; true when it worked. */
export async function copyText(value: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(value)
    toast.success('已复制')
    return true
  } catch {
    toast.error('复制失败，请手动选择文本复制')
    return false
  }
}
