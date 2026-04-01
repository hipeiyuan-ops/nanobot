/**
 * TypeScript 类型声明文件
 *
 * 为没有自带类型定义的第三方模块提供类型声明。
 */

/**
 * qrcode-terminal 模块类型声明
 *
 * 该模块用于在终端中生成 ASCII QR 码。
 * 用于 WhatsApp 登录时显示扫描二维码。
 */
declare module 'qrcode-terminal' {
  /**
   * 在终端中生成 QR 码
   *
   * @param text - 要编码的文本内容（通常是 WhatsApp 认证链接）
   * @param options - 可选配置
   * @param options.small - 是否使用小尺寸 QR 码，默认 false
   *
   * @example
   * // 生成标准 QR 码
   * qrcode.generate('https://example.com');
   *
   * // 生成小尺寸 QR 码
   * qrcode.generate('https://example.com', { small: true });
   */
  export function generate(text: string, options?: { small?: boolean }): void;
}
