import { theme } from 'antd'

const tdesignFontFamily =
  "TCloudNumber, -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Microsoft YaHei', Arial, sans-serif"

const darkTheme = {
  token: {
    fontFamily: tdesignFontFamily,
    colorPrimary: '#6366f1',
    colorBgBase: '#1c1f2e',
    colorTextBase: '#f1f5f9',
    colorBgContainer: '#1c1f2e',
    colorBgElevated: '#252836',
    colorBorder: 'rgba(255,255,255,0.15)',
    borderRadius: 8,
    colorText: '#f1f5f9',
    colorTextSecondary: '#b0bcd4',
    colorTextTertiary: '#7a8ba3',
    colorBgLayout: '#13151e',
    colorBgSpotlight: 'rgba(99,102,241,0.2)',
  },
  components: {
    Layout: {
      siderBg: '#1c1f2e',
      triggerBg: '#1c1f2e',
      triggerColor: '#f1f5f9',
    },
  },
  algorithm: theme.darkAlgorithm,
}

const lightTheme = {
  token: {
    fontFamily: tdesignFontFamily,
    colorPrimary: '#4f46e5',
    colorBgBase: '#ffffff',
    colorTextBase: '#0f172a',
    colorBgContainer: '#ffffff',
    colorBgElevated: '#ffffff',
    colorBorder: 'rgba(0,0,0,0.1)',
    borderRadius: 8,
    colorText: '#0f172a',
    colorTextSecondary: '#475569',
    colorTextTertiary: '#94a3b8',
    colorBgLayout: '#f8fafc',
  },
  components: {
    Layout: {
      siderBg: '#ffffff',
      triggerBg: '#ffffff',
      triggerColor: '#0f172a',
    },
  },
  algorithm: theme.defaultAlgorithm,
}

export { darkTheme, lightTheme, tdesignFontFamily }
