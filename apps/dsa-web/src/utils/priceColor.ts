/**
 * 涨跌配色工具：统一跟随系统设置 MARKET_REVIEW_COLOR_SCHEME 的红绿习惯。
 *
 * - green_up（默认）：绿涨红跌
 * - red_up：红涨绿跌
 *
 * 持仓页、大盘报告概览、历史趋势抽屉都通过这里取色，保证全站一致。
 */

export type MarketReviewColorScheme = 'green_up' | 'red_up';

export const DEFAULT_COLOR_SCHEME: MarketReviewColorScheme = 'green_up';

export const COLOR_SCHEME_OPTIONS: MarketReviewColorScheme[] = ['green_up', 'red_up'];

const SUCCESS = 'hsl(var(--color-success))';
const DANGER = 'hsl(var(--color-danger))';

export function normalizeColorScheme(value: string | undefined | null): MarketReviewColorScheme {
  return value === 'red_up' ? 'red_up' : DEFAULT_COLOR_SCHEME;
}

/** 涨（正值）对应的 CSS 颜色。 */
export function getPriceUpColor(scheme: MarketReviewColorScheme = DEFAULT_COLOR_SCHEME): string {
  return scheme === 'red_up' ? DANGER : SUCCESS;
}

/** 跌（负值）对应的 CSS 颜色。 */
export function getPriceDownColor(scheme: MarketReviewColorScheme = DEFAULT_COLOR_SCHEME): string {
  return scheme === 'red_up' ? SUCCESS : DANGER;
}

/**
 * 根据带符号的数值返回内联颜色（用于 style 绑定）。
 * 0、null、undefined 返回 undefined（由上层沿用默认色）。
 */
export function getPriceChangeColor(
  value: number | undefined | null,
  scheme: MarketReviewColorScheme = DEFAULT_COLOR_SCHEME,
): string | undefined {
  if (typeof value !== 'number' || !Number.isFinite(value) || value === 0) {
    return undefined;
  }
  return value > 0 ? getPriceUpColor(scheme) : getPriceDownColor(scheme);
}

/**
 * 根据带符号的数值返回 Tailwind 颜色类名（用于 className 绑定）。
 * 与持仓页原有语义保持一致：>= 0 视为涨（含 0 盈利），无价格为中性色。
 */
export function getPriceChangeClassName(
  value: number | undefined | null,
  scheme: MarketReviewColorScheme = DEFAULT_COLOR_SCHEME,
): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return 'text-secondary';
  }
  if (value >= 0) {
    return scheme === 'red_up' ? 'text-danger' : 'text-success';
  }
  return scheme === 'red_up' ? 'text-success' : 'text-danger';
}
