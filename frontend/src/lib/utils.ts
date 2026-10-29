import {clsx, type ClassValue} from 'clsx'
import {twMerge} from 'tailwind-merge'
import {toast} from 'sonner'
import {t} from '@/i18n'
export function cn(...inputs: ClassValue[]) {return twMerge(clsx(inputs))}

/** Copy a value, with a toast either way; true when it worked. */
export async function copyText(value: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(value)
    toast.success(t('common.copy.done'))
    return true
  } catch {
    toast.error(t('common.copy.failed'))
    return false
  }
}
