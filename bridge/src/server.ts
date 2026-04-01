/**
 * WebSocket 服务器，用于 Python-Node.js 桥接通信。
 *
 * 安全特性：
 *   - 仅绑定到 127.0.0.1，不暴露到外部网络
 *   - 可选的 BRIDGE_TOKEN 认证
 *
 * 消息协议：
 *   入站（Python -> Node.js）：
 *     - { type: 'send', to: string, text: string } - 发送文本消息
 *     - { type: 'send_media', to: string, filePath: string, mimetype: string, caption?: string, fileName?: string } - 发送媒体
 *     - { type: 'auth', token: string } - 认证握手
 *
 *   出站（Node.js -> Python）：
 *     - { type: 'message', ... } - 收到新消息
 *     - { type: 'status', status: string } - 连接状态变化
 *     - { type: 'qr', qr: string } - QR 码数据
 *     - { type: 'error', error: string } - 错误信息
 *     - { type: 'sent', to: string } - 消息发送确认
 */

import { WebSocketServer, WebSocket } from 'ws';
import { WhatsAppClient, InboundMessage } from './whatsapp.js';

/**
 * 发送文本消息命令
 *
 * @property type - 命令类型，固定为 'send'
 * @property to - 目标 WhatsApp JID（用户或群组标识符）
 * @property text - 要发送的文本内容
 */
interface SendCommand {
  type: 'send';
  to: string;
  text: string;
}

/**
 * 发送媒体消息命令
 *
 * @property type - 命令类型，固定为 'send_media'
 * @property to - 目标 WhatsApp JID
 * @property filePath - 本地媒体文件路径
 * @property mimetype - 媒体 MIME 类型（如 image/jpeg, video/mp4）
 * @property caption - 可选的媒体标题/说明
 * @property fileName - 可选的文件名（用于文档类型）
 */
interface SendMediaCommand {
  type: 'send_media';
  to: string;
  filePath: string;
  mimetype: string;
  caption?: string;
  fileName?: string;
}

/** 桥接命令联合类型 */
type BridgeCommand = SendCommand | SendMediaCommand;

/**
 * 桥接消息类型
 *
 * @property type - 消息类型
 *   - 'message': 收到新消息
 *   - 'status': 连接状态变化
 *   - 'qr': QR 码数据
 *   - 'error': 错误信息
 */
interface BridgeMessage {
  type: 'message' | 'status' | 'qr' | 'error';
  [key: string]: unknown;
}

/**
 * 桥接服务器类
 *
 * 管理 WebSocket 连接和 WhatsApp 客户端，
 * 在 Python 后端和 WhatsApp 之间转发消息。
 */
export class BridgeServer {
  /** WebSocket 服务器实例 */
  private wss: WebSocketServer | null = null;
  /** WhatsApp 客户端实例 */
  private wa: WhatsAppClient | null = null;
  /** 已连接的 WebSocket 客户端集合 */
  private clients: Set<WebSocket> = new Set();

  /**
   * 创建桥接服务器实例
   *
   * @param port - WebSocket 服务端口
   * @param authDir - WhatsApp 认证数据存储目录
   * @param token - 可选的认证令牌，用于保护 WebSocket 连接
   */
  constructor(private port: number, private authDir: string, private token?: string) {}

  /**
   * 启动桥接服务器
   *
   * 初始化 WebSocket 服务器并连接 WhatsApp
   */
  async start(): Promise<void> {
    // 仅绑定到 localhost — 永不暴露到外部网络
    this.wss = new WebSocketServer({ host: '127.0.0.1', port: this.port });
    console.log(`🌉 Bridge server listening on ws://127.0.0.1:${this.port}`);
    if (this.token) console.log('🔒 Token authentication enabled');

    // 初始化 WhatsApp 客户端
    this.wa = new WhatsAppClient({
      authDir: this.authDir,
      onMessage: (msg) => this.broadcast({ type: 'message', ...msg }),
      onQR: (qr) => this.broadcast({ type: 'qr', qr }),
      onStatus: (status) => this.broadcast({ type: 'status', status }),
    });

    // 处理 WebSocket 连接
    this.wss.on('connection', (ws) => {
      if (this.token) {
        // 要求第一条消息为认证握手
        // 如果 5 秒内未收到有效认证，关闭连接
        const timeout = setTimeout(() => ws.close(4001, 'Auth timeout'), 5000);
        ws.once('message', (data) => {
          clearTimeout(timeout);
          try {
            const msg = JSON.parse(data.toString());
            if (msg.type === 'auth' && msg.token === this.token) {
              console.log('🔗 Python client authenticated');
              this.setupClient(ws);
            } else {
              ws.close(4003, 'Invalid token');
            }
          } catch {
            ws.close(4003, 'Invalid auth message');
          }
        });
      } else {
        // 无令牌认证，直接接受连接
        console.log('🔗 Python client connected');
        this.setupClient(ws);
      }
    });

    // 连接到 WhatsApp
    await this.wa.connect();
  }

  /**
   * 设置客户端连接处理
   *
   * 注册消息处理、关闭和错误事件处理器
   *
   * @param ws - WebSocket 连接
   */
  private setupClient(ws: WebSocket): void {
    // 将客户端添加到集合
    this.clients.add(ws);

    // 处理来自 Python 的命令
    ws.on('message', async (data) => {
      try {
        const cmd = JSON.parse(data.toString()) as BridgeCommand;
        await this.handleCommand(cmd);
        // 发送确认消息
        ws.send(JSON.stringify({ type: 'sent', to: cmd.to }));
      } catch (error) {
        console.error('Error handling command:', error);
        ws.send(JSON.stringify({ type: 'error', error: String(error) }));
      }
    });

    // 处理连接关闭
    ws.on('close', () => {
      console.log('🔌 Python client disconnected');
      this.clients.delete(ws);
    });

    // 处理连接错误
    ws.on('error', (error) => {
      console.error('WebSocket error:', error);
      this.clients.delete(ws);
    });
  }

  /**
   * 处理来自 Python 的命令
   *
   * @param cmd - 桥接命令
   */
  private async handleCommand(cmd: BridgeCommand): Promise<void> {
    if (!this.wa) return;

    if (cmd.type === 'send') {
      // 发送文本消息
      await this.wa.sendMessage(cmd.to, cmd.text);
    } else if (cmd.type === 'send_media') {
      // 发送媒体消息
      await this.wa.sendMedia(cmd.to, cmd.filePath, cmd.mimetype, cmd.caption, cmd.fileName);
    }
  }

  /**
   * 向所有连接的客户端广播消息
   *
   * @param msg - 要广播的消息
   */
  private broadcast(msg: BridgeMessage): void {
    const data = JSON.stringify(msg);
    for (const client of this.clients) {
      if (client.readyState === WebSocket.OPEN) {
        client.send(data);
      }
    }
  }

  /**
   * 停止桥接服务器
   *
   * 关闭所有连接并清理资源
   */
  async stop(): Promise<void> {
    // 关闭所有客户端连接
    for (const client of this.clients) {
      client.close();
    }
    this.clients.clear();

    // 关闭 WebSocket 服务器
    if (this.wss) {
      this.wss.close();
      this.wss = null;
    }

    // 断开 WhatsApp 连接
    if (this.wa) {
      await this.wa.disconnect();
      this.wa = null;
    }
  }
}
