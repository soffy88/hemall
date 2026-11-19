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

interface PickOrder {
  id: string;
  order_no: string;
  customer_name: string;
  customer_phone: string;
  status: string;
  lines: PickOrderLine[];
  created_at: string;
}

interface PickOrderLine {
  id: string;
  variant_id: string;
  name: string;
  sku: string;
  quantity: number;
  unit: string;
  price_cents: number;
  pricing_mode: 'piece' | 'weight';
  estimated_weight_grams?: number;
  actual_weight_grams?: number;
  estimated_amount_cents: number;
  actual_amount_cents?: number;
  diff_cents?: number;
  settled: boolean;
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

export default function PickWeighPage() {
  const showToast = useToast();
  const [devices, setDevices] = useState<ScaleDevice[]>([]);
  const [selectedDeviceId, setSelectedDeviceId] = useState<string>('');
  const [connected, setConnected] = useState(false);
  const [currentWeight, setCurrentWeight] = useState(0);
  const [stableWeight, setStableWeight] = useState<StableWeight | null>(null);
  const [candidates, setCandidates] = useState<RecognitionCandidate[]>([]);
  const [recognizing, setRecognizing] = useState(false);
  const [orders, setOrders] = useState<PickOrder[]>([]);
  const [selectedOrder, setSelectedOrder] = useState<PickOrder | null>(null);
  const [currentLineIndex, setCurrentLineIndex] = useState(0);
  const [simulatorMode, setSimulatorMode] = useState(false);
  const [simulatorWeight, setSimulatorWeight] = useState(0);
  const [loading, setLoading] = useState(false);

  const serialPortRef = useRef<SerialPort | null>(null);
  const readerRef = useRef<ReadableStreamDefaultReader<Uint8Array> | null>(null);
  const weightWindowRef = useRef<ScaleReading[]>([]);
  const STABLE_COUNT = 3;
  const STABLE_THRESHOLD_G = 2;

  // 加载秤设备
  useEffect(() => {
    loadDevices();
  }, []);

  // 加载待拣货订单
  useEffect(() => {
    loadPickOrders();
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

  async function loadPickOrders() {
    try {
      const res = await api.scalePickOrders();
      setOrders(res.orders);
    } catch (e) {
      showToast('加载拣货单失败: ' + String(e), 'error');
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

  function handleScaleLine(line: string) {
    api.scaleParseLine(line, selectedDeviceId)
      .then((res) => {
        if (res.reading) {
          const reading: ScaleReading = res.reading;
          setCurrentWeight(reading.weight_grams);
          weightWindowRef.current.push(reading);
          if (weightWindowRef.current.length > STABLE_COUNT * 3) {
            weightWindowRef.current.shift();
          }
          if (isStable(weightWindowRef.current)) {
            const stable = getStableWeight(weightWindowRef.current);
            if (stable) {
              setStableWeight({ ...stable, source: 'scale' });
              recognizeFrame(stable.weight_grams);
            }
          }
        }
      })
      .catch(() => {});
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

  async function recognizeFrame(weightGrams: number) {
    if (!stableWeight || recognizing || !selectedOrder) return;
    const currentLine = selectedOrder.lines[currentLineIndex];
    if (!currentLine || currentLine.settled) return;

    setRecognizing(true);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: 'environment', width: { ideal: 1280 }, height: { ideal: 720 } },
      });
      const video = document.createElement('video');
      video.srcObject = stream;
      await video.play();
      await new Promise<void>(resolve => { video.onloadeddata = () => resolve(); });
      const canvas = document.createElement('canvas');
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      const ctx = canvas.getContext('2d')!;
      ctx.drawImage(video, 0, 0);
      const blob = await new Promise<Blob>(resolve => canvas.toBlob(b => resolve(b!), 'image/jpeg', 0.85));
      stream.getTracks().forEach(t => t.stop());

      const res = await api.scaleRecognizePick(
        new File([blob], 'frame.jpg', { type: 'image/jpeg' }),
        weightGrams,
        currentLine.variant_id
      );
      setCandidates(res.candidates || []);
    } catch (e) {
      showToast('识别失败: ' + String(e), 'error');
      setCandidates([]);
    } finally {
      setRecognizing(false);
    }
  }

  // 选择订单
  function selectOrder(order: PickOrder) {
    setSelectedOrder(order);
    setCurrentLineIndex(0);
    setCandidates([]);
    setStableWeight(null);
    weightWindowRef.current = [];
    setCurrentWeight(0);
  }

  // 确认识别结果并记录实称重量
  async function confirmPick(candidate?: RecognitionCandidate) {
    if (!selectedOrder || !stableWeight) return;
    const line = selectedOrder.lines[currentLineIndex];
    if (!line) return;

    const weight = stableWeight.weight_grams;
    if (weight <= 0) {
      showToast('重量必须大于 0', 'error');
      return;
    }

    // 如果有候选，校验是否拿对货
    if (candidate && candidate.variant_id !== line.variant_id) {
      const confirmed = window.confirm(
        `识别结果为 "${candidate.name}" (${candidate.sku})，与预期商品 "${line.name}" 不符。\n确定要按识别结果记录吗？`
      );
      if (!confirmed) return;
    }

    setLoading(true);
    try {
      await api.scaleRecordPickWeight({
        order_id: selectedOrder.id,
        line_id: line.id,
        weight_grams: weight,
        confirmed_variant_id: candidate?.variant_id || line.variant_id,
      });
      showToast(`已记录: ${line.name} ${(weight / 1000).toFixed(3)} kg`, 'success');

      // 移动到下一行
      const nextIndex = selectedOrder.lines.findIndex((l, i) => i > currentLineIndex && !l.settled);
      if (nextIndex >= 0) {
        setCurrentLineIndex(nextIndex);
      } else {
        // 全部完成，刷新订单状态
        showToast('拣货完成！', 'success');
        await loadPickOrders();
        setSelectedOrder(null);
      }
      // 重置秤状态
      setStableWeight(null);
      weightWindowRef.current = [];
      setCurrentWeight(0);
      setCandidates([]);
    } catch (e: any) {
      showToast('记录失败: ' + (e.response?.data?.detail || e.message), 'error');
    } finally {
      setLoading(false);
    }
  }

  // 跳过当前行（人工处理）
  async function skipLine() {
    if (!selectedOrder) return;
    const line = selectedOrder.lines[currentLineIndex];
    if (!line) return;

    const reason = prompt(`跳过 "${line.name}"，请填写原因:`);
    if (reason === null) return;

    setLoading(true);
    try {
      await api.scaleSkipPickLine({
        order_id: selectedOrder.id,
        line_id: line.id,
        reason,
      });
      showToast('已跳过，标记为人工处理', 'info');
      const nextIndex = selectedOrder.lines.findIndex((l, i) => i > currentLineIndex && !l.settled);
      if (nextIndex >= 0) {
        setCurrentLineIndex(nextIndex);
      } else {
        showToast('拣货完成！', 'success');
        await loadPickOrders();
        setSelectedOrder(null);
      }
    } catch (e) {
      showToast('跳过失败: ' + String(e), 'error');
    } finally {
      setLoading(false);
    }
  }

  const currentLine = selectedOrder?.lines[currentLineIndex];
  const progress = selectedOrder
    ? selectedOrder.lines.filter(l => l.settled).length
    : 0;
  const totalLines = selectedOrder?.lines.length || 0;

  return (
    <div className="max-w-7xl mx-auto space-y-6">
      {/* 顶部：订单选择与进度 */}
      <div className="bg-white rounded-xl border border-gray-200 p-4">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold text-gray-900">📦 拣货称重</h3>
          <button
            onClick={loadPickOrders}
            disabled={loading}
            className="px-3 py-1.5 text-sm bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition disabled:opacity-50"
          >
            刷新订单
          </button>
        </div>

        {selectedOrder && (
          <div className="mb-4 p-3 bg-emerald-50 rounded-lg border border-emerald-100">
            <div className="flex items-center justify-between">
              <div>
                <div className="font-medium text-gray-900">订单: {selectedOrder.order_no}</div>
                <div className="text-sm text-gray-500">客户: {selectedOrder.customer_name} / {selectedOrder.customer_phone}</div>
              </div>
              <div className="text-right">
                <div className="text-2xl font-bold text-emerald-700">{progress} / {totalLines}</div>
                <div className="text-sm text-gray-500">已完成 / 总行数</div>
              </div>
            </div>
            <div className="mt-2 h-2 bg-gray-200 rounded-full overflow-hidden">
              <div
                className="h-full bg-emerald-600 transition-all duration-300"
                style={{ width: totalLines > 0 ? `${(progress / totalLines) * 100}%` : '0%' }}
              />
            </div>
          </div>
        )}

        {!selectedOrder ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {orders.map(order => (
              <button
                key={order.id}
                onClick={() => selectOrder(order)}
                className={`p-4 border rounded-lg text-left transition ${
                  order.status === 'pending_pick' ? 'border-emerald-300 bg-emerald-50' : 'border-gray-200'
                }`}
              >
                <div className="font-medium text-gray-900">{order.order_no}</div>
                <div className="text-sm text-gray-500">{order.customer_name}</div>
                <div className="text-xs text-gray-400 mt-1">{order.lines.length} 个商品</div>
                <div className="text-xs mt-1 px-2 py-0.5 rounded-full bg-gray-100 text-gray-600">
                  {order.status}
                </div>
              </button>
            ))}
            {orders.length === 0 && (
              <div className="col-span-full text-center py-8 text-gray-400">
                暂无待拣货订单
              </div>
            )}
          </div>
        ) : (
          <button
            onClick={() => setSelectedOrder(null)}
            className="w-full py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition"
          >
            ← 返回订单列表
          </button>
        )}
      </div>

      {selectedOrder && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* 左侧：秤与识别 */}
          <div className="lg:col-span-2 space-y-4">
            {/* 当前拣货商品 */}
            <div className="bg-white rounded-xl border border-gray-200 p-4">
              <h3 className="text-lg font-semibold text-gray-900 mb-4">🎯 当前拣货商品</h3>
              {currentLine ? (
                <div className="p-4 bg-gray-50 rounded-lg">
                  <div className="font-medium text-lg text-gray-900">{currentLine.name}</div>
                  <div className="text-sm text-gray-500 mt-1">SKU: {currentLine.sku}</div>
                  <div className="text-sm text-gray-500 mt-1">
                    预期: {currentLine.pricing_mode === 'weight'
                      ? `${(currentLine.estimated_weight_grams || 0) / 1000} kg`
                      : `${currentLine.quantity} ${currentLine.unit}`}
                  </div>
                  <div className="text-sm text-gray-500 mt-1">
                    单价: {formatCurrency(currentLine.price_cents)}/{currentLine.pricing_mode === 'weight' ? '斤' : '件'}
                  </div>
                  {currentLine.actual_weight_grams && (
                    <div className="mt-2 text-sm text-emerald-700">
                      已称重: {(currentLine.actual_weight_grams / 1000).toFixed(3)} kg
                    </div>
                  )}
                  {currentLine.diff_cents !== undefined && (
                    <div className={`mt-2 text-sm font-medium ${currentLine.diff_cents > 0 ? 'text-red-600' : currentLine.diff_cents < 0 ? 'text-green-600' : 'text-gray-600'}`}>
                      差额: {currentLine.diff_cents > 0 ? '+' : ''}{formatCurrency(currentLine.diff_cents)}
                    </div>
                  )}
                </div>
              ) : (
                <div className="text-center py-8 text-gray-400">所有商品已拣货完成</div>
              )}
            </div>

            {/* 秤连接 */}
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
                      {d.name} ({d.port_path}, {d.baud_rate})
                    </option>
                  ))}
                </select>
                {!connected ? (
                  <button
                    onClick={connectScale}
                    disabled={!selectedDeviceId}
                    className="px-4 py-2 bg-emerald-700 text-white rounded-lg hover:bg-emerald-800 transition disabled:opacity-50"
                  >
                    连接秤
                  </button>
                ) : (
                  <button
                    onClick={disconnectScale}
                    className="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 transition"
                  >
                    断开
                  </button>
                )}
                <label className="flex items-center gap-2 text-sm text-gray-600">
                  <input
                    type="checkbox"
                    checked={simulatorMode}
                    onChange={e => setSimulatorMode(e.target.checked)}
                    className="rounded border-gray-300 text-emerald-600 focus:ring-emerald-500"
                  />
                  模拟器
                </label>
              </div>
              {simulatorMode && (
                <div className="flex items-center gap-4 p-3 bg-gray-50 rounded-lg mb-4">
                  <label className="text-sm text-gray-600">模拟重量:</label>
                  <input
                    type="number"
                    value={simulatorWeight}
                    onChange={e => { setSimulatorWeight(Number(e.target.value)); handleSimulatorWeightChange(); }}
                    min={0} max={15000} step={1}
                    className="w-32 px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500"
                  />
                </div>
              )}
              {/* 重量显示 */}
              <div className={`p-4 rounded-xl text-center text-4xl font-mono font-bold ${stableWeight ? 'bg-green-50 text-green-800 border-2 border-green-300' : 'bg-gray-50 text-gray-600'}`}>
                {currentWeight.toLocaleString()} g
                {stableWeight && <span className="ml-3 text-lg font-normal text-green-600">● 稳定</span>}
              </div>
            </div>

            {/* 识别候选 */}
            <div className="bg-white rounded-xl border border-gray-200 p-4">
              <h3 className="text-lg font-semibold text-gray-900 mb-4">📷 识别校验</h3>
              {recognizing && <div className="mb-3 text-sm text-emerald-600">正在识别...</div>}
              {candidates.length === 0 && !recognizing && (
                <div className="text-center py-6 text-gray-400">
                  重量稳定后自动拍摄识别，校验是否拿对货
                </div>
              )}
              <div className="space-y-2">
                {candidates.map(c => (
                  <button
                    key={c.variant_id}
                    onClick={() => confirmPick(c)}
                    disabled={loading}
                    className={`w-full p-3 border rounded-lg text-left transition ${
                      c.variant_id === currentLine?.variant_id
                        ? 'border-emerald-400 bg-emerald-50'
                        : 'border-gray-200 hover:border-red-300 hover:bg-red-50'
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <div>
                        <div className="font-medium text-gray-900">{c.name}</div>
                        <div className="text-sm text-gray-500">{c.sku}</div>
                      </div>
                      <span className="text-sm font-medium text-emerald-700">{(c.score * 100).toFixed(1)}%</span>
                    </div>
                    {c.variant_id !== currentLine?.variant_id && (
                      <div className="text-xs text-red-600 mt-1">⚠ 与预期商品不符</div>
                    )}
                  </button>
                ))}
              </div>
              {currentLine && currentLine.settled && (
                <div className="mt-3 p-3 bg-green-50 border border-green-200 rounded-lg text-green-800 text-center">
                  ✓ 已记录实称重量
                </div>
              )}
            </div>
          </div>

          {/* 右侧：拣货清单与操作 */}
          <div className="space-y-4">
            <div className="bg-white rounded-xl border border-gray-200 p-4 sticky top-24">
              <h3 className="text-lg font-semibold text-gray-900 mb-4">📋 拣货清单</h3>
              <div className="space-y-2 max-h-[500px] overflow-y-auto">
                {selectedOrder.lines.map((line, idx) => (
                  <button
                    key={line.id}
                    onClick={() => setCurrentLineIndex(idx)}
                    disabled={loading}
                    className={`w-full p-3 rounded-lg text-left transition ${
                      idx === currentLineIndex ? 'bg-emerald-50 border-2 border-emerald-400' : 'bg-gray-50 border border-gray-200 hover:bg-gray-100'
                    } ${line.settled ? 'opacity-60' : ''}`}
                  >
                    <div className="flex items-center justify-between">
                      <div className="flex-1 min-w-0">
                        <div className={`font-medium truncate ${line.settled ? 'line-through text-gray-400' : 'text-gray-900'}`}>
                          {line.name}
                        </div>
                        <div className="text-xs text-gray-500">{line.sku}</div>
                      </div>
                      <div className="text-right ml-2">
                        {line.settled ? (
                          <span className="text-xs text-green-600">✓ 完成</span>
                        ) : idx === currentLineIndex ? (
                          <span className="text-xs text-emerald-600 font-medium">▶ 进行中</span>
                        ) : (
                          <span className="text-xs text-gray-400">待拣货</span>
                        )}
                      </div>
                    </div>
                    <div className="text-sm text-gray-500 mt-1">
                      预期: {line.pricing_mode === 'weight'
                        ? `${(line.estimated_weight_grams || 0) / 1000} kg`
                        : `${line.quantity} ${line.unit}`}
                    </div>
                    {line.actual_weight_grams && (
                      <div className="text-sm text-emerald-700 mt-1">
                        实称: {(line.actual_weight_grams / 1000).toFixed(3)} kg
                      </div>
                    )}
                  </button>
                ))}
              </div>
              {/* 操作按钮 */}
              <div className="mt-4 space-y-2">
                {currentLine && !currentLine.settled && (
                  <>
                    <button
                      onClick={() => confirmPick()}
                      disabled={!stableWeight || loading}
                      className="w-full py-2 bg-emerald-700 text-white rounded-lg font-medium hover:bg-emerald-800 transition disabled:opacity-50"
                    >
                      {loading ? '记录中...' : `确认记录 (${stableWeight ? (stableWeight.weight_grams / 1000).toFixed(3) + ' kg' : '等待稳定重量'})`}
                    </button>
                    <button
                      onClick={skipLine}
                      disabled={loading}
                      className="w-full py-2 bg-gray-200 text-gray-700 rounded-lg font-medium hover:bg-gray-300 transition disabled:opacity-50"
                    >
                      跳过 (人工处理)
                    </button>
                  </>
                )}
                {currentLine && currentLine.settled && (
                  <div className="text-center text-sm text-green-600">
                    该商品已完成拣货称重
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}