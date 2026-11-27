'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '@/lib/api-client';
import { useToast } from '@/components/Toast';
import { formatCurrency } from '@/lib/format';

// 禁止静态生成：需要认证和 Web Serial API
export const dynamic = 'force-dynamic';

// Web Serial API 类型声明
interface SerialPort extends EventTarget {
  readonly readable: ReadableStream<Uint8Array> | null;
  readonly writable: WritableStream<Uint8Array> | null;
  open(options: SerialOptions): Promise<void>;
  close(): Promise<void>;
  getSignals(): Promise<SerialOutputSignals>;
  setSignals(signals: SerialInputSignals): Promise<void>;
}
interface SerialOptions {
  baudRate: number;
  dataBits?: number;
  stopBits?: number;
  parity?: 'none' | 'even' | 'odd';
  bufferSize?: number;
  flowControl?: 'none' | 'hardware';
}
interface SerialOutputSignals {
  dataTerminalReady: boolean;
  requestToSend: boolean;
  break: boolean;
}
interface SerialInputSignals {
  dataCarrierDetect: boolean;
  clearToSend: boolean;
  ringIndicator: boolean;
  dataSetReady: boolean;
}
interface SerialPortRequestOptions {
  filters?: Array<{ usbVendorId?: number; usbProductId?: number }>;
}
interface Navigator {
  serial?: {
    requestPort(options?: SerialPortRequestOptions): Promise<SerialPort>;
    getPorts(): Promise<SerialPort[]>;
  };
}

interface ScaleReading {
  weight_grams: number;
  unit: 'g' | 'kg';
  raw: string;
  stable: boolean;
  timestamp: number;
}

interface StableWeight {
  weight_grams: number;
  readings: ScaleReading[];
  source: 'scale' | 'simulator' | 'bridge';
}

interface RecognitionCandidate {
  variant_id: string;
  score: number;
  name: string;
  sku: string;
  price_cents: number;
  pricing_mode: 'piece' | 'weight';
  image_url?: string;
}

interface TicketLine {
  id: string;
  variant_id: string;
  name: string;
  sku: string;
  weight_grams: number;
  unit_price_cents: number;
  price_basis_grams: number;
  amount_cents: number;
  pricing_mode: 'piece' | 'weight';
}

interface ScaleDevice {
  id: string;
  name: string;
  port_path: string;
  baud_rate: number;
  protocol: string;
  status: string;
}

const JIN_GRAMS = 500;

export default function POSPage() {
  const showToast = useToast();
  const [devices, setDevices] = useState<ScaleDevice[]>([]);
  const [selectedDeviceId, setSelectedDeviceId] = useState<string>('');
  const [connected, setConnected] = useState(false);
  const [currentWeight, setCurrentWeight] = useState(0);
  const [stableWeight, setStableWeight] = useState<StableWeight | null>(null);
  const [candidates, setCandidates] = useState<RecognitionCandidate[]>([]);
  const [recognizing, setRecognizing] = useState(false);
  const [ticketLines, setTicketLines] = useState<TicketLine[]>([]);
  const [customerId, setCustomerId] = useState<string>('');
  const [paymentMethod, setPaymentMethod] = useState<'cash' | 'qr'>('cash');
  const [cashReceived, setCashReceived] = useState(0);
  const [settling, setSettling] = useState(false);
  const [simulatorMode, setSimulatorMode] = useState(false);
  const [simulatorWeight, setSimulatorWeight] = useState(0);

  const serialPortRef = useRef<SerialPort | null>(null);
  const readerRef = useRef<ReadableStreamDefaultReader<Uint8Array> | null>(null);
  const weightWindowRef = useRef<ScaleReading[]>([]);
  const STABLE_COUNT = 3;
  const STABLE_THRESHOLD_G = 2;

  // 加载秤设备列表
  useEffect(() => {
    loadDevices();
  }, []);

  async function loadDevices() {
    try {
      const data = await api.scaleListDevices();
      setDevices(data);
      if (data.length > 0 && !selectedDeviceId) {
        setSelectedDeviceId(data[0].id);
      }
    } catch (e) {
      showToast('加载秤设备失败: ' + String(e), 'error');
    }
  }

  // Web Serial 连接
  async function connectScale() {
    if (!selectedDeviceId) {
      showToast('请先选择秤设备', 'error');
      return;
    }
    if (!('serial' in navigator)) {
      showToast('当前浏览器不支持 Web Serial API，请使用 Chrome/Edge 桌面版，或启用本地桥接程序', 'error');
      return;
    }
    try {
      const port = await (navigator as any).serial.requestPort();
      await port.open({ baudRate: 9600, dataBits: 8, stopBits: 1, parity: 'none' });
      serialPortRef.current = port;
      readerRef.current = port.readable!.getReader();
      setConnected(true);
      showToast('秤连接成功', 'success');
      readLoop();
    } catch (e: any) {
      if (e.name !== 'NotFoundError') {
        showToast('连接失败: ' + e.message, 'error');
      }
    }
  }

  async function disconnectScale() {
    try {
      if (readerRef.current) {
        await readerRef.current.cancel();
        readerRef.current.releaseLock();
        readerRef.current = null;
      }
      if (serialPortRef.current) {
        await serialPortRef.current.close();
        serialPortRef.current = null;
      }
      setConnected(false);
      setCurrentWeight(0);
      setStableWeight(null);
      weightWindowRef.current = [];
      showToast('已断开连接', 'info');
    } catch (e) {
      showToast('断开失败: ' + String(e), 'error');
    }
  }

  // 读取循环
  async function readLoop() {
    const reader = readerRef.current;
    if (!reader) return;
    const decoder = new TextDecoder();
    let buffer = '';
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed) continue;
          handleScaleLine(trimmed);
        }
      }
    } catch (e: any) {
      if (e.name !== 'AbortError') {
        console.error('Serial read error:', e);
        showToast('读取串口出错: ' + e.message, 'error');
      }
    }
  }

  // 处理秤数据行
  function handleScaleLine(line: string) {
    // 发送给后端解析（后端统一协议解析）
    api.scaleParseLine(line, selectedDeviceId)
      .then((res) => {
        if (res.reading) {
          const reading: ScaleReading = res.reading;
          setCurrentWeight(reading.weight_grams);
          weightWindowRef.current.push(reading);
          if (weightWindowRef.current.length > STABLE_COUNT * 3) {
            weightWindowRef.current.shift();
          }
          // 稳定判定
          if (isStable(weightWindowRef.current)) {
            const stable = getStableWeight(weightWindowRef.current);
            if (stable) {
              setStableWeight({ ...stable, source: 'scale' });
              // 自动触发识别
              recognizeFrame(stable.weight_grams);
            }
          }
        }
      })
      .catch(() => {
        // 解析失败静默忽略
      });
  }

  function isStable(window: ScaleReading[]): boolean {
    if (window.length < STABLE_COUNT) return false;
    const recent = window.slice(-STABLE_COUNT);
    for (let i = 1; i < recent.length; i++) {
      if (Math.abs(recent[i].weight_grams - recent[i - 1].weight_grams) > STABLE_THRESHOLD_G) {
        return false;
      }
    }
    return true;
  }

  function getStableWeight(window: ScaleReading[]): StableWeight | null {
    if (!isStable(window)) return null;
    const recent = window.slice(-STABLE_COUNT);
    const weights = recent.map(r => r.weight_grams).sort((a, b) => a - b);
    const median = weights[Math.floor(weights.length / 2)];
    return {
      weight_grams: median,
      readings: recent,
      source: 'scale',
    };
  }

  // 模拟器模式
  function handleSimulatorWeightChange() {
    setCurrentWeight(simulatorWeight);
    const reading: ScaleReading = {
      weight_grams: simulatorWeight,
      unit: 'g',
      raw: `SIM ${simulatorWeight}g`,
      stable: false,
      timestamp: Date.now(),
    };
    weightWindowRef.current.push(reading);
    if (weightWindowRef.current.length > STABLE_COUNT * 3) {
      weightWindowRef.current.shift();
    }
    if (isStable(weightWindowRef.current)) {
      const stable = getStableWeight(weightWindowRef.current);
      if (stable) {
        setStableWeight({ ...stable, source: 'simulator' });
        recognizeFrame(stable.weight_grams);
      }
    }
  }

  // 摄像头识别
  async function recognizeFrame(weightGrams: number) {
    if (!stableWeight || recognizing) return;
    setRecognizing(true);
    try {
      // 获取摄像头帧
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: 'environment', width: { ideal: 1280 }, height: { ideal: 720 } },
      });
      const video = document.createElement('video');
      video.srcObject = stream;
      await video.play();
      // 等待视频准备好
      await new Promise<void>(resolve => {
        video.onloadeddata = () => resolve();
      });
      // 绘制到 canvas 并转 blob
      const canvas = document.createElement('canvas');
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      const ctx = canvas.getContext('2d')!;
      ctx.drawImage(video, 0, 0);
      const blob = await new Promise<Blob>(resolve => canvas.toBlob(b => resolve(b!), 'image/jpeg', 0.85));
      stream.getTracks().forEach(t => t.stop());

      // 上传识别
      const res = await api.scaleRecognize(new File([blob], 'frame.jpg', { type: 'image/jpeg' }), weightGrams);
      setCandidates(res.candidates || []);
    } catch (e) {
      showToast('识别失败: ' + String(e), 'error');
      setCandidates([]);
    } finally {
      setRecognizing(false);
    }
  }

  // 选择候选商品加入收银单
  async function addCandidateToTicket(candidate: RecognitionCandidate) {
    if (stableWeight === null) return;
    const weight = stableWeight.weight_grams;
    if (weight <= 0) {
      showToast('重量必须大于 0', 'error');
      return;
    }
    // 计价
    let amount = 0;
    if (candidate.pricing_mode === 'weight') {
      const basis = JIN_GRAMS; // 默认按斤计价
      amount = Math.round((weight * candidate.price_cents) / basis);
    } else {
      amount = candidate.price_cents;
    }
    const line: TicketLine = {
      id: crypto.randomUUID(),
      variant_id: candidate.variant_id,
      name: candidate.name,
      sku: candidate.sku,
      weight_grams: candidate.pricing_mode === 'weight' ? weight : 0,
      unit_price_cents: candidate.price_cents,
      price_basis_grams: JIN_GRAMS,
      amount_cents: amount,
      pricing_mode: candidate.pricing_mode,
    };
    setTicketLines(prev => [...prev, line]);
    // 清除稳定重量，准备下一笔
    setStableWeight(null);
    weightWindowRef.current = [];
    setCurrentWeight(0);
    setCandidates([]);
    showToast('已加入收银单', 'success');
  }

  // 手工搜索商品
  async function searchProduct(query: string) {
    if (!query.trim()) return;
    try {
      const res = await api.scaleSearchProduct(query);
      if (res.products && res.products.length > 0) {
        setCandidates(res.products.map((p) => ({
          variant_id: p.variant_id,
          score: 1,
          name: p.name,
          sku: p.sku,
          price_cents: p.price_cents,
          pricing_mode: p.pricing_mode,
          image_url: p.image_url,
        })));
      } else {
        showToast('未找到商品', 'info');
      }
    } catch (e) {
      showToast('搜索失败: ' + String(e), 'error');
    }
  }

  // 移除收银单行
  function removeLine(id: string) {
    setTicketLines(prev => prev.filter(l => l.id !== id));
  }

  // 计算合计
  const totalAmount = ticketLines.reduce((sum, l) => sum + l.amount_cents, 0);
  const change = cashReceived - totalAmount;

  // 结算
  async function settle() {
    if (ticketLines.length === 0) {
      showToast('收银单为空', 'error');
      return;
    }
    if (paymentMethod === 'cash' && cashReceived < totalAmount) {
      showToast('收款金额不足', 'error');
      return;
    }
    setSettling(true);
    try {
      const res = await api.scaleSettle({
        lines: ticketLines.map(l => ({
          variant_id: l.variant_id,
          weight_grams: l.weight_grams,
          unit_price_cents: l.unit_price_cents,
          price_basis_grams: l.price_basis_grams,
          amount_cents: l.amount_cents,
          pricing_mode: l.pricing_mode,
        })),
        customer_id: customerId || null,
        payment_method: paymentMethod,
        cash_received: cashReceived,
      });
      showToast(`结算成功，订单号: ${res.order_no}`, 'success');
      // 打印小票
      printReceipt(res);
      // 清空单据
      setTicketLines([]);
      setCustomerId('');
      setCashReceived(0);
    } catch (e: any) {
      showToast('结算失败: ' + (e.response?.data?.detail || e.message), 'error');
    } finally {
      setSettling(false);
    }
  }

  // 打印小票 (58mm 热敏打印)
  function printReceipt(order: any) {
    const win = window.open('', '_blank', 'width=300,height=600');
    if (!win) {
      showToast('请允许弹窗以打印小票', 'error');
      return;
    }
    const linesHtml = order.lines.map((l: any) =>
      `<div class="line">${l.name} x${l.quantity || 1} ${formatCurrency(l.amount_cents)}</div>`
    ).join('');
    win.document.write(`
      <html><head><title>小票</title>
      <style>
        body{font-family:monospace;font-size:12px;padding:10px;width:240px;margin:0}
        .center{text-align:center}.bold{font-weight:bold}
        .line{display:flex;justify-content:space-between;margin:4px 0}
        .total{border-top:1px dashed #000;padding-top:8px;margin-top:8px;font-weight:bold}
        .qr{width:120px;height:120px;margin:8px auto}
      </style>
      </head><body>
        <div class="center bold">宋鲜生 社区生鲜店</div>
        <div class="center">收银小票</div>
        <div>单号: ${order.order_no}</div>
        <div>时间: ${new Date().toLocaleString()}</div>
        <div>------------------------------</div>
        ${linesHtml}
        <div class="total line">合计: ${formatCurrency(totalAmount)}</div>
        <div class="line">付款: ${formatCurrency(cashReceived)}</div>
        <div class="line">找零: ${formatCurrency(change)}</div>
        <div>------------------------------</div>
        <div class="center">谢谢惠顾</div>
      </body></html>
    `);
    win.document.close();
    win.focus();
    setTimeout(() => win.print(), 500);
  }

  // 去皮/清零
  async function tare() {
    if (serialPortRef.current) {
      // 发送去皮指令（协议相关，此处仅示例）
      const writer = serialPortRef.current.writable?.getWriter();
      if (writer) {
        await writer.write(new TextEncoder().encode('TARE\r\n'));
        writer.releaseLock();
      }
    }
    showToast('去皮指令已发送', 'info');
  }

  async function zero() {
    if (serialPortRef.current) {
      const writer = serialPortRef.current.writable?.getWriter();
      if (writer) {
        await writer.write(new TextEncoder().encode('ZERO\r\n'));
        writer.releaseLock();
      }
    }
    showToast('清零指令已发送', 'info');
  }

  return (
    <div className="max-w-7xl mx-auto space-y-6">
      {/* 顶部：秤连接与状态 */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 space-y-4">
          {/* 秤连接卡片 */}
          <div className="bg-white rounded-xl border border-gray-200 p-4">
            <h3 className="text-lg font-semibold text-gray-900 mb-4 flex items-center gap-2">
              📟 电子秤连接
              {connected && <span className="w-2 h-2 bg-green-500 rounded-full" />}
              {!connected && <span className="w-2 h-2 bg-red-500 rounded-full" />}
            </h3>
            <div className="flex flex-wrap items-center gap-4 mb-4">
              <select
                value={selectedDeviceId}
                onChange={e => setSelectedDeviceId(e.target.value)}
                className="flex-1 min-w-[200px] px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500"
              >
                {devices.map(d => (
                  <option key={d.id} value={d.id}>
                    {d.name} ({d.port_path}, {d.baud_rate}, {d.protocol})
                  </option>
                ))}
              </select>
              {!connected ? (
                <button
                  onClick={connectScale}
                  disabled={!selectedDeviceId}
                  className="px-4 py-2 bg-emerald-700 text-white rounded-lg hover:bg-emerald-800 transition disabled:opacity-50"
                >
                  连接秤 (Web Serial)
                </button>
              ) : (
                <button
                  onClick={disconnectScale}
                  className="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 transition"
                >
                  断开连接
                </button>
              )}
              <label className="flex items-center gap-2 text-sm text-gray-600">
                <input
                  type="checkbox"
                  checked={simulatorMode}
                  onChange={e => setSimulatorMode(e.target.checked)}
                  className="rounded border-gray-300 text-emerald-600 focus:ring-emerald-500"
                />
                模拟器模式
              </label>
            </div>
            {simulatorMode && (
              <div className="flex items-center gap-4 p-3 bg-gray-50 rounded-lg">
                <label className="text-sm text-gray-600">模拟重量 (克):</label>
                <input
                  type="number"
                  value={simulatorWeight}
                  onChange={e => { setSimulatorWeight(Number(e.target.value)); handleSimulatorWeightChange(); }}
                  min={0}
                  max={15000}
                  step={1}
                  className="w-32 px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500"
                />
                <button
                  onClick={tare}
                  className="px-3 py-2 text-sm bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300 transition"
                >
                  去皮
                </button>
                <button
                  onClick={zero}
                  className="px-3 py-2 text-sm bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300 transition"
                >
                  清零
                </button>
              </div>
            )}
            {/* 当前重量显示 */}
            <div className={`p-4 rounded-xl text-center text-4xl font-mono font-bold ${stableWeight ? 'bg-green-50 text-green-800 border-2 border-green-300' : 'bg-gray-50 text-gray-600'}`}>
              {currentWeight.toLocaleString()} g
              {stableWeight && <span className="ml-3 text-lg font-normal text-green-600">● 稳定</span>}
            </div>
          </div>

          {/* 识别候选 */}
          <div className="bg-white rounded-xl border border-gray-200 p-4">
            <h3 className="text-lg font-semibold text-gray-900 mb-4">📷 识别候选 (重量稳定时自动拍摄)</h3>
            {recognizing && <div className="mb-3 text-sm text-emerald-600">正在识别...</div>}
            {candidates.length === 0 && !recognizing && (
              <div className="text-center py-8 text-gray-400">
                等待稳定重量后自动拍摄识别，或手工搜索商品
              </div>
            )}
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {candidates.map((c, idx) => (
                <button
                  key={c.variant_id}
                  onClick={() => addCandidateToTicket(c)}
                  className="p-3 border border-gray-200 rounded-lg hover:border-emerald-400 hover:bg-emerald-50 transition text-left"
                >
                  <div className="font-medium text-gray-900">{c.name}</div>
                  <div className="text-sm text-gray-500">{c.sku}</div>
                  <div className="text-sm text-gray-500">{c.pricing_mode === 'weight' ? '按重量计价' : '按件计价'}</div>
                  <div className="text-lg font-bold text-emerald-700 mt-1">{formatCurrency(c.price_cents)}/{c.pricing_mode === 'weight' ? '斤' : '件'}</div>
                  <div className="text-xs text-gray-400 mt-1">置信度: {(c.score * 100).toFixed(1)}%</div>
                </button>
              ))}
            </div>
            {/* 手工搜索 */}
            <div className="mt-4 flex gap-2">
              <input
                type="text"
                placeholder="拼音/名称/SKU 搜索商品..."
                onKeyDown={e => e.key === 'Enter' && searchProduct((e.target as HTMLInputElement).value)}
                className="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500"
              />
              <button
                onClick={() => searchProduct((document.querySelector('input[placeholder*="拼音"]') as HTMLInputElement)?.value || '')}
                className="px-4 py-2 bg-emerald-700 text-white rounded-lg hover:bg-emerald-800 transition"
              >
                搜索
              </button>
            </div>
          </div>
        </div>

        {/* 右侧：收银单 */}
        <div className="space-y-4">
          <div className="bg-white rounded-xl border border-gray-200 p-4 sticky top-24">
            <h3 className="text-lg font-semibold text-gray-900 mb-4">🧾 收银单</h3>
            {ticketLines.length === 0 ? (
              <div className="text-center py-8 text-gray-400">暂无商品</div>
            ) : (
              <div className="space-y-3 max-h-[400px] overflow-y-auto">
                {ticketLines.map(line => (
                  <div key={line.id} className="flex items-center justify-between p-3 bg-gray-50 rounded-lg">
                    <div className="flex-1 min-w-0">
                      <div className="font-medium text-gray-900 truncate">{line.name}</div>
                      <div className="text-sm text-gray-500">
                        {line.pricing_mode === 'weight'
                          ? `${(line.weight_grams / 1000).toFixed(3)} kg × ${formatCurrency(line.unit_price_cents)}/斤`
                          : `1 件 × ${formatCurrency(line.unit_price_cents)}`}
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="font-bold text-lg text-emerald-700">{formatCurrency(line.amount_cents)}</span>
                      <button
                        onClick={() => removeLine(line.id)}
                        className="p-1 text-gray-400 hover:text-red-600 transition"
                        title="删除"
                      >
                        ✕
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
            <div className="mt-4 border-t border-gray-200 pt-4 space-y-2">
              <div className="flex justify-between text-lg">
                <span>合计</span>
                <span className="font-bold text-emerald-700">{formatCurrency(totalAmount)}</span>
              </div>
              {/* 顾客选择 */}
              <div className="flex gap-2">
                <input
                  type="text"
                  placeholder="顾客手机/ID (可选)"
                  value={customerId}
                  onChange={e => setCustomerId(e.target.value)}
                  className="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500"
                />
              </div>
              {/* 支付方式 */}
              <div className="flex gap-2">
                <label className="flex-1 flex items-center gap-2">
                  <input
                    type="radio"
                    name="payment"
                    value="cash"
                    checked={paymentMethod === 'cash'}
                    onChange={() => setPaymentMethod('cash')}
                    className="text-emerald-600 focus:ring-emerald-500"
                  />
                  💵 现金
                </label>
                <label className="flex-1 flex items-center gap-2">
                  <input
                    type="radio"
                    name="payment"
                    value="qr"
                    checked={paymentMethod === 'qr'}
                    onChange={() => setPaymentMethod('qr')}
                    className="text-emerald-600 focus:ring-emerald-500"
                  />
                  📱 到店扫码记录
                </label>
              </div>
              {paymentMethod === 'cash' && (
                <div className="flex gap-2">
                  <input
                    type="number"
                    placeholder="收款金额 (分)"
                    value={cashReceived}
                    onChange={e => setCashReceived(Number(e.target.value) || 0)}
                    min={totalAmount}
                    step={1}
                    className="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500"
                  />
                </div>
              )}
              {paymentMethod === 'cash' && cashReceived >= totalAmount && (
                <div className="flex justify-between text-sm text-gray-600">
                  <span>找零</span>
                  <span className="font-medium">{formatCurrency(change)}</span>
                </div>
              )}
              <button
                onClick={settle}
                disabled={ticketLines.length === 0 || settling || (paymentMethod === 'cash' && cashReceived < totalAmount)}
                className="w-full py-3 bg-emerald-700 text-white rounded-lg font-semibold text-lg hover:bg-emerald-800 transition disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {settling ? '结算中...' : `结算 (${formatCurrency(totalAmount)})`}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}