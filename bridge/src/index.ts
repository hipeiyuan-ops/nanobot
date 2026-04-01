#!/usr/bin/env node
/**
 * nanobot WhatsApp 桥接服务入口
 *
 * 该桥接服务连接 WhatsApp Web 与 nanobot 的 Python 后端，
 * 通过 WebSocket 进行通信。处理认证、消息转发和重连逻辑。
 *
 * 使用方式：
 *   npm run build && npm start
 *
 * 或使用自定义设置：
 *   BRIDGE_PORT=3001 AUTH_DIR=~/.nanobot/whatsapp npm start
 *
 * 环境变量：
 *   BRIDGE_PORT: WebSocket 服务端口（默认 3001）
 *   AUTH_DIR: WhatsApp 认证数据存储目录
 *   BRIDGE_TOKEN: 可选的认证令牌
 */

// 为 Baileys 在 ESM 模式下提供 crypto polyfill
// Baileys 需要 Web Crypto API，而 Node.js 默认不提供
import { webcrypto } from 'crypto';
if (!globalThis.crypto) {
  (globalThis as any).crypto = webcrypto;
}

import { BridgeServer } from './server.js';
import { homedir } from 'os';
import { join } from 'path';

// 从环境变量读取配置，使用默认值
const PORT = parseInt(process.env.BRIDGE_PORT || '3001', 10);
const AUTH_DIR = process.env.AUTH_DIR || join(homedir(), '.nanobot', 'whatsapp-auth');
const TOKEN = process.env.BRIDGE_TOKEN || undefined;

console.log('🐈 nanobot WhatsApp Bridge');
console.log('========================\n');

// 创建桥接服务器实例
const server = new BridgeServer(PORT, AUTH_DIR, TOKEN);

// 处理优雅关闭（Ctrl+C）
process.on('SIGINT', async () => {
  console.log('\n\nShutting down...');
  await server.stop();
  process.exit(0);
});

// 处理终止信号
process.on('SIGTERM', async () => {
  await server.stop();
  process.exit(0);
});

// 启动服务器
server.start().catch((error) => {
  console.error('Failed to start bridge:', error);
  process.exit(1);
});
