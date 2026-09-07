import { theme } from 'antd'

const tdesignFontFamily =
  "TCloudNumber, -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Microsoft YaHei', Arial, sans-serif"

const darkTheme = {
  token: {
    fontFamily: tdesignFontFamily,
    colorPrimary: '#6366f1',
    colorBgBase: '#1b1d23',
    colorTextBase: '#eceef3',
    colorBgContainer: '#1b1d23',
    colorBgElevated: '#22252d',
    colorBorder: '#343842',
    borderRadius: 8,
    colorText: '#eceef3',
    colorTextSecondary: '#a6adbb',
    colorTextTertiary: '#a0a8b7',
    colorTextPlaceholder: '#929bab',
    colorLink: '#a3a5ff',
    colorLinkHover: '#c0c1ff',
    colorSuccess: '#69ba86',
    colorError: '#ff8a92',
    colorWarning: '#e1b867',
    colorBgLayout: '#131418',
    colorBgSpotlight: 'rgba(99,102,241,0.2)',
  },
  components: {
    Layout: {
      siderBg: '#1b1d23',
      triggerBg: '#1b1d23',
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
    colorTextBase: '#20242e',
    colorBgContainer: '#ffffff',
    colorBgElevated: '#ffffff',
    colorBorder: '#dce0e7',
    borderRadius: 8,
    colorText: '#20242e',
    colorTextSecondary: '#626a79',
    colorTextTertiary: '#626a79',
    colorTextPlaceholder: '#687284',
    colorLink: '#4f46e5',
    colorLinkHover: '#4338ca',
    colorSuccess: '#237a3d',
    colorError: '#c6323a',
    colorWarning: '#97620b',
    colorBgLayout: '#f5f6f8',
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
