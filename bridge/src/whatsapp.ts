/**
 * WhatsApp 客户端封装，使用 Baileys 库。
 *
 * 基于 OpenClaw 的实现，提供与 WhatsApp Web 的通信能力。
 *
 * 主要功能：
 *   - QR 码扫码登录
 *   - 消息收发（文本、图片、视频、文档）
 *   - 群聊消息处理
 *   - @ 提及检测
 *   - 媒体文件下载
 *   - 自动重连
 *
 * 依赖：
 *   - @whiskeysockets/baileys: WhatsApp Web 协议实现
 *   - qrcode-terminal: 终端 QR 码显示
 *   - pino: 日志记录
 */

/* eslint-disable @typescript-eslint/no-explicit-any */
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  downloadMediaMessage,
  extractMessageContent as baileysExtractMessageContent,
} from '@whiskeysockets/baileys';

import { Boom } from '@hapi/boom';
import qrcode from 'qrcode-terminal';
import pino from 'pino';
import { readFile, writeFile, mkdir } from 'fs/promises';
import { join, basename } from 'path';
import { randomBytes } from 'crypto';

/** 桥接版本号 */
const VERSION = '0.1.0';

/**
 * 入站消息结构
 *
 * 从 WhatsApp 接收并转发给 Python 后端的消息格式。
 */
export interface InboundMessage {
  /** 消息 ID，WhatsApp 内部唯一标识符 */
  id: string;
  /** 发送者 JID（WhatsApp 用户/群组标识符） */
  sender: string;
  /** 推送名称（发送者显示名称） */
  pn: string;
  /** 消息文本内容 */
  content: string;
  /** 消息时间戳（Unix 秒） */
  timestamp: number;
  /** 是否为群聊消息 */
  isGroup: boolean;
  /** 是否被 @ 提及（仅群聊有效） */
  wasMentioned?: boolean;
  /** 媒体文件本地路径列表 */
  media?: string[];
}

/**
 * WhatsApp 客户端配置选项
 */
export interface WhatsAppClientOptions {
  /** 认证数据存储目录，用于保存登录状态 */
  authDir: string;
  /** 收到消息时的回调函数 */
  onMessage: (msg: InboundMessage) => void;
  /** QR 码生成时的回调函数 */
  onQR: (qr: string) => void;
  /** 连接状态变化时的回调函数 */
  onStatus: (status: string) => void;
}

/**
 * WhatsApp 客户端类
 *
 * 封装 Baileys 库，提供简化的消息收发接口。
 */
export class WhatsAppClient {
  /** Baileys socket 连接实例 */
  private sock: any = null;
  /** 客户端配置选项 */
  private options: WhatsAppClientOptions;
  /** 是否正在重连中，防止重复重连 */
  private reconnecting = false;

  /**
   * 创建 WhatsApp 客户端实例
   *
   * @param options - 客户端配置选项
   */
  constructor(options: WhatsAppClientOptions) {
    this.options = options;
  }

  /**
   * 标准化 JID（移除设备后缀）
   *
   * WhatsApp JID 格式为 "user@domain:device"，此方法移除 ":device" 部分。
   * 这是因为同一用户在不同设备上的 JID 后缀不同，需要标准化才能正确比较。
   *
   * @param jid - 原始 JID
   * @returns 标准化后的 JID
   */
  private normalizeJid(jid: string | undefined | null): string {
    return (jid || '').split(':')[0];
  }

  /**
   * 检查机器人是否在消息中被 @ 提及
   *
   * 检查各种消息类型中的 mentionedJid 列表，
   * 判断当前登录的机器人账号是否被提及。
   *
   * @param msg - 消息对象
   * @returns 被提及返回 true
   */
  private wasMentioned(msg: any): boolean {
    // 仅群聊消息可能有提及
    if (!msg?.key?.remoteJid?.endsWith('@g.us')) return false;

    // 收集所有可能的提及列表（不同消息类型的提及位置不同）
    const candidates = [
      msg?.message?.extendedTextMessage?.contextInfo?.mentionedJid,
      msg?.message?.imageMessage?.contextInfo?.mentionedJid,
      msg?.message?.videoMessage?.contextInfo?.mentionedJid,
      msg?.message?.documentMessage?.contextInfo?.mentionedJid,
      msg?.message?.audioMessage?.contextInfo?.mentionedJid,
    ];
    const mentioned = candidates.flatMap((items) => (Array.isArray(items) ? items : []));
    if (mentioned.length === 0) return false;

    // 获取自己的所有可能 ID（不同设备可能有不同的 ID 格式）
    const selfIds = new Set(
      [this.sock?.user?.id, this.sock?.user?.lid, this.sock?.user?.jid]
        .map((jid) => this.normalizeJid(jid))
        .filter(Boolean),
    );
    return mentioned.some((jid: string) => selfIds.has(this.normalizeJid(jid)));
  }

  /**
   * 连接到 WhatsApp
   *
   * 初始化 Baileys socket 并设置事件处理器。
   * 首次连接时会显示 QR 码供扫码登录。
   */
  async connect(): Promise<void> {
    // 创建静默日志记录器
    const logger = pino({ level: 'silent' });
    // 加载或创建认证状态
    const { state, saveCreds } = await useMultiFileAuthState(this.options.authDir);
    // 获取最新的 Baileys 协议版本
    const { version } = await fetchLatestBaileysVersion();

    console.log(`Using Baileys version: ${version.join('.')}`);

    // 按照 OpenClaw 的模式创建 socket
    this.sock = makeWASocket({
      auth: {
        creds: state.creds,
        keys: makeCacheableSignalKeyStore(state.keys, logger),
      },
      version,
      logger,
      printQRInTerminal: false, // 我们自己处理 QR 码显示
      browser: ['nanobot', 'cli', VERSION],
      syncFullHistory: false, // 不同步完整历史，节省资源
      markOnlineOnConnect: false, // 连接时不标记在线
    });

    // 处理 WebSocket 错误
    if (this.sock.ws && typeof this.sock.ws.on === 'function') {
      this.sock.ws.on('error', (err: Error) => {
        console.error('WebSocket error:', err.message);
      });
    }

    // 处理连接状态更新
    this.sock.ev.on('connection.update', async (update: any) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        // 在终端显示 QR 码
        console.log('\n📱 Scan this QR code with WhatsApp (Linked Devices):\n');
        qrcode.generate(qr, { small: true });
        // 通知 Python 后端 QR 码数据
        this.options.onQR(qr);
      }

      if (connection === 'close') {
        // 连接关闭，检查原因
        const statusCode = (lastDisconnect?.error as Boom)?.output?.statusCode;
        // 如果不是被登出，则尝试重连
        const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

        console.log(`Connection closed. Status: ${statusCode}, Will reconnect: ${shouldReconnect}`);
        this.options.onStatus('disconnected');

        // 自动重连（除非被登出）
        if (shouldReconnect && !this.reconnecting) {
          this.reconnecting = true;
          console.log('Reconnecting in 5 seconds...');
          setTimeout(() => {
            this.reconnecting = false;
            this.connect();
          }, 5000);
        }
      } else if (connection === 'open') {
        // 连接成功
        console.log('✅ Connected to WhatsApp');
        this.options.onStatus('connected');
      }
    });

    // 凭据更新时保存到磁盘
    this.sock.ev.on('creds.update', saveCreds);

    // 处理收到的消息
    this.sock.ev.on('messages.upsert', async ({ messages, type }: { messages: any[]; type: string }) => {
      // 仅处理新消息通知
      if (type !== 'notify') return;

      for (const msg of messages) {
        // 跳过自己发送的消息
        if (msg.key.fromMe) continue;
        // 跳过状态广播（WhatsApp 状态/动态）
        if (msg.key.remoteJid === 'status@broadcast') continue;

        // 解包消息内容
        const unwrapped = baileysExtractMessageContent(msg.message);
        if (!unwrapped) continue;

        // 提取文本内容
        const content = this.getTextContent(unwrapped);
        let fallbackContent: string | null = null;
        const mediaPaths: string[] = [];

        // 处理不同类型的媒体消息
        if (unwrapped.imageMessage) {
          fallbackContent = '[Image]';
          const path = await this.downloadMedia(msg, unwrapped.imageMessage.mimetype ?? undefined);
          if (path) mediaPaths.push(path);
        } else if (unwrapped.documentMessage) {
          fallbackContent = '[Document]';
          const path = await this.downloadMedia(msg, unwrapped.documentMessage.mimetype ?? undefined,
            unwrapped.documentMessage.fileName ?? undefined);
          if (path) mediaPaths.push(path);
        } else if (unwrapped.videoMessage) {
          fallbackContent = '[Video]';
          const path = await this.downloadMedia(msg, unwrapped.videoMessage.mimetype ?? undefined);
          if (path) mediaPaths.push(path);
        }

        // 确定最终内容
        const finalContent = content || (mediaPaths.length === 0 ? fallbackContent : '') || '';
        if (!finalContent && mediaPaths.length === 0) continue;

        // 判断是否为群聊消息
        const isGroup = msg.key.remoteJid?.endsWith('@g.us') || false;
        // 检查是否被 @ 提及
        const wasMentioned = this.wasMentioned(msg);

        // 通过回调转发消息到 Python 后端
        this.options.onMessage({
          id: msg.key.id || '',
          sender: msg.key.remoteJid || '',
          pn: msg.key.remoteJidAlt || '',
          content: finalContent,
          timestamp: msg.messageTimestamp as number,
          isGroup,
          ...(isGroup ? { wasMentioned } : {}),
          ...(mediaPaths.length > 0 ? { media: mediaPaths } : {}),
        });
      }
    });
  }

  /**
   * 下载媒体文件到本地
   *
   * 将 WhatsApp 服务器上的媒体文件下载到本地磁盘。
   *
   * @param msg - 消息对象
   * @param mimetype - MIME 类型
   * @param fileName - 文件名（可选，文档类型通常有文件名）
   * @returns 本地文件路径，失败返回 null
   */
  private async downloadMedia(msg: any, mimetype?: string, fileName?: string): Promise<string | null> {
    try {
      // 创建媒体存储目录
      const mediaDir = join(this.options.authDir, '..', 'media');
      await mkdir(mediaDir, { recursive: true });

      // 下载媒体数据到缓冲区
      const buffer = await downloadMediaMessage(msg, 'buffer', {}) as Buffer;

      // 生成输出文件名
      let outFilename: string;
      if (fileName) {
        // 文档类型有文件名 — 使用唯一前缀避免冲突
        const prefix = `wa_${Date.now()}_${randomBytes(4).toString('hex')}_`;
        outFilename = prefix + fileName;
      } else {
        // 从 MIME 类型派生扩展名（如 "image/png" → ".png"）
        const mime = mimetype || 'application/octet-stream';
        const ext = '.' + (mime.split('/').pop()?.split(';')[0] || 'bin');
        outFilename = `wa_${Date.now()}_${randomBytes(4).toString('hex')}${ext}`;
      }

      // 写入文件
      const filepath = join(mediaDir, outFilename);
      await writeFile(filepath, buffer);

      return filepath;
    } catch (err) {
      console.error('Failed to download media:', err);
      return null;
    }
  }

  /**
   * 从消息对象中提取文本内容
   *
   * 支持多种消息类型：
   *   - 纯文本消息
   *   - 扩展文本（回复、链接预览）
   *   - 图片/视频/文档（带标题）
   *   - 语音消息
   *
   * @param message - 消息对象
   * @returns 文本内容，无文本返回 null
   */
  private getTextContent(message: any): string | null {
    // 纯文本消息
    if (message.conversation) {
      return message.conversation;
    }

    // 扩展文本（回复、链接预览）
    if (message.extendedTextMessage?.text) {
      return message.extendedTextMessage.text;
    }

    // 图片（带可选标题）
    if (message.imageMessage) {
      return message.imageMessage.caption || '';
    }

    // 视频（带可选标题）
    if (message.videoMessage) {
      return message.videoMessage.caption || '';
    }

    // 文档（带可选标题）
    if (message.documentMessage) {
      return message.documentMessage.caption || '';
    }

    // 语音/音频消息
    if (message.audioMessage) {
      return `[Voice Message]`;
    }

    return null;
  }

  /**
   * 发送文本消息
   *
   * @param to - 目标 JID（用户或群组）
   * @param text - 消息文本内容
   * @throws 如果未连接则抛出错误
   */
  async sendMessage(to: string, text: string): Promise<void> {
    if (!this.sock) {
      throw new Error('Not connected');
    }

    await this.sock.sendMessage(to, { text });
  }

  /**
   * 发送媒体消息
   *
   * 根据媒体类型自动选择发送方式：
   *   - 图片：使用 image 类型
   *   - 视频：使用 video 类型
   *   - 音频：使用 audio 类型
   *   - 其他：使用 document 类型
   *
   * @param to - 目标 JID
   * @param filePath - 本地文件路径
   * @param mimetype - MIME 类型
   * @param caption - 可选标题
   * @param fileName - 可选文件名（文档类型）
   * @throws 如果未连接则抛出错误
   */
  async sendMedia(
    to: string,
    filePath: string,
    mimetype: string,
    caption?: string,
    fileName?: string,
  ): Promise<void> {
    if (!this.sock) {
      throw new Error('Not connected');
    }

    // 读取文件内容
    const buffer = await readFile(filePath);
    // 根据 MIME 类型分类
    const category = mimetype.split('/')[0];

    if (category === 'image') {
      // 发送图片
      await this.sock.sendMessage(to, { image: buffer, caption: caption || undefined, mimetype });
    } else if (category === 'video') {
      // 发送视频
      await this.sock.sendMessage(to, { video: buffer, caption: caption || undefined, mimetype });
    } else if (category === 'audio') {
      // 发送音频
      await this.sock.sendMessage(to, { audio: buffer, mimetype });
    } else {
      // 发送文档
      const name = fileName || basename(filePath);
      await this.sock.sendMessage(to, { document: buffer, mimetype, fileName: name });
    }
  }

  /**
   * 断开 WhatsApp 连接
   *
   * 关闭 socket 连接并清理资源
   */
  async disconnect(): Promise<void> {
    if (this.sock) {
      this.sock.end(undefined);
      this.sock = null;
    }
  }
}
