import { useEffect, useState } from 'react';
import { systemConfigApi } from '../api/systemConfig';
import {
  DEFAULT_COLOR_SCHEME,
  normalizeColorScheme,
  type MarketReviewColorScheme,
} from '../utils/priceColor';

const CONFIG_KEY = 'MARKET_REVIEW_COLOR_SCHEME';

type ConfigItem = { key: string; value?: string };

function readSchemeFromItems(items: ConfigItem[] | undefined): MarketReviewColorScheme {
  if (!Array.isArray(items)) {
    return DEFAULT_COLOR_SCHEME;
  }
  const item = items.find((entry) => entry.key === CONFIG_KEY);
  return normalizeColorScheme(item?.value);
}

/**
 * 读取系统设置 MARKET_REVIEW_COLOR_SCHEME，作为全站涨跌配色的唯一来源。
 * 每次挂载都会重新拉取，确保用户在设置页修改后切回能拿到最新值。
 */
export function useMarketReviewColorScheme(): MarketReviewColorScheme {
  const [scheme, setScheme] = useState<MarketReviewColorScheme>(DEFAULT_COLOR_SCHEME);

  useEffect(() => {
    let active = true;
    systemConfigApi
      .getConfig(false)
      .then((config) => {
        if (active) {
          setScheme(readSchemeFromItems(config.items));
        }
      })
      .catch(() => {
        if (active) {
          setScheme(DEFAULT_COLOR_SCHEME);
        }
      });
    return () => {
      active = false;
    };
  }, []);

  return scheme;
}
