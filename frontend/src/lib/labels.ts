/**
 * The Russian a person reads, and the one place vocabularies are translated.
 *
 * Two of these maps are *ours* and closed — the match buckets, the run statuses
 * — so an unknown key in them is a bug and shows as the raw value rather than
 * as a friendly guess.
 *
 * One of them is not ours at all. `hh_last_state` is hh's own vocabulary, it is
 * open, and hh adds to it; the backend passes it through verbatim for exactly
 * that reason. So :func:`outcomeLabel` translates what it recognises and shows
 * the rest as hh wrote it. A state we have never seen must reach the screen
 * looking like a state we have never seen, not like "нет ответа".
 */

import type { ApplicationStatus, Bucket, ParseStatus, Remote, RunStatus } from '@/types/api'

export const BUCKETS: Record<Bucket, string> = {
  apply_now: 'откликаться сейчас',
  strong: 'сильное совпадение',
  stretch: 'дотянуться можно',
  skip: 'мимо',
  filtered: 'отсеяно фильтром',
}

export const RUN_STATUS: Record<RunStatus, string> = {
  running: 'идёт',
  success: 'успех',
  partial: 'частично',
  failed: 'сбой',
}

export const PARSE_STATUS: Record<ParseStatus, string> = {
  pending: 'разбирается',
  ready: 'разобрано',
  failed: 'не разобрано',
}

export const REMOTE: Record<Remote, string> = {
  no: 'офис',
  hybrid: 'гибрид',
  full: 'удалённо',
}

export const APPLICATION_STATUS: Record<ApplicationStatus, string> = {
  saved: 'отложено',
  applied: 'отправлено',
  screening: 'скрининг',
  interview: 'интервью',
  offer: 'оффер',
  rejected: 'отказ',
}

/** The board's own columns: where an application is in *this* project. */
export const STAGES: Record<string, string> = {
  queued: 'в очереди',
  needs_manual: 'нужен человек',
  sent_unconfirmed: 'hh не подтвердил',
  sent: 'отправлено',
  other: 'вне очереди',
}

export const STAGE_NOTES: Record<string, string> = {
  queued: 'Письмо написано. Нажмите «Откликнуться…», прочитайте карточку и подтвердите.',
  needs_manual: 'Агент остановился и оставил причину.',
  sent_unconfirmed:
    'Агент сообщил об отправке, но подтверждения от hh в базе нет. «Обновить исходы откликов» перечитает эти вакансии на hh.',
  sent: 'hh подтвердил: его счётчик откликов не ноль или он сообщил состояние переписки.',
  other: 'Без письма и без отправки: заметки, заведённые руками, и вакансии, которые агент пропустил.',
}

/** What hh has said, grouped by the backend into four answers plus a fallback. */
export const OUTCOMES: Record<string, string> = {
  viewed: 'просмотрен',
  waiting: 'ожидание',
  invitation: 'приглашение',
  rejection: 'отказ',
  other: 'другое',
}

/** hh's own state names, for the ones that have actually been observed. */
const HH_STATES: Record<string, string> = {
  RESPONSE: 'просмотрен',
  VIEWED: 'просмотрен',
  PENDING: 'ожидание',
  NEW: 'новый',
  INVITATION: 'приглашение',
  INTERVIEW: 'интервью',
  PHONE_INTERVIEW: 'телефонное интервью',
  DISCARD: 'отказ',
  REJECTED: 'отказ',
}

export function outcomeLabel(state: string | null): string | null {
  if (!state) return null
  // Unknown states are shown as hh wrote them. hh's vocabulary is open, and a
  // fallback of "нет ответа" would turn an outcome we have not seen before into
  // the absence of one.
  return HH_STATES[state] ?? state
}

export const EVIDENCE: Record<string, string> = {
  other_profile: 'есть в другом резюме',
  resume_text: 'названо в тексте этого CV',
}

/**
 * Where a requirement came from — a different question from the three columns
 * around it. Those say what the candidate has; this says whether anybody asked.
 * Only the inferred half is labelled: a stated requirement is the normal case
 * and marking every one of them would bury the one line worth reading.
 */
export const REQUIREMENT_SOURCE: Record<string, string> = {
  description_text: 'выведено из текста описания',
}

export const SKILL_LEVEL: Record<string, string> = {
  basic: 'базово',
  working: 'уверенно',
  strong: 'сильно',
  expert: 'экспертно',
}

export const ATS_OVERALL: Record<string, string> = {
  ok: 'читается',
  degraded: 'читается частично',
  unreadable: 'не читается',
}

export const ATS_SEVERITY: Record<string, string> = {
  critical: 'критично',
  warning: 'предупреждение',
  info: 'к сведению',
}

/** Why the workshop wrote nothing. Each is an answer, not a failure. */
export const SKIPPED: Record<string, string> = {
  vacancy_not_found: 'Вакансия исчезла из базы.',
  letter_exists: 'Письмо уже написано — нажмите «переписать», чтобы заменить.',
  dry_run: 'Пробный запуск: ничего не сохранено.',
  letter_unwritable: 'Ни один вариант не прошёл проверки. Ничего не сохранено.',
  no_active_profile: 'Нет активного резюме.',
}
