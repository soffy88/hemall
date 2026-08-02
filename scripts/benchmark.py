#!/usr/bin/env python3
"""
Hemall 性能基准测试脚本

此脚本用于测试 Hemall API 在不同负载下的性能表现。
"""

import asyncio
import time
import statistics
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List, Dict, Any
import httpx
import json


@dataclass
class BenchmarkResult:
    """基准测试结果"""
    endpoint: str
    total_requests: int
    successful_requests: int
    failed_requests: int
    total_time: float
    rps: float
    avg_response_time: float
    median_response_time: float
    p95_response_time: float
    p99_response_time: float
    error_rate: float


class HemallBenchmark:
    """Hemall 性能基准测试类"""
    
    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = base_url
        self.client = httpx.AsyncClient(timeout=30.0)
        self.results: List[BenchmarkResult] = []
        
    async def benchmark_endpoint(
        self, 
        endpoint: str, 
        method: str = "GET",
        concurrency: int = 50,
        duration: int = 60,
        body: Dict[Any, Any] = None
    ) -> BenchmarkResult:
        """测试单个端点的性能"""
        print(f"开始测试端点: {method} {endpoint}")
        
        start_time = time.time()
        request_count = 0
        successful_requests = 0
        failed_requests = 0
        response_times = []
        
        # 用于跟踪并发请求的任务
        tasks = set()
        
        async def make_request():
            nonlocal request_count, successful_requests, failed_requests
            try:
                req_start = time.time()
                
                if method.upper() == "GET":
                    response = await self.client.get(f"{self.base_url}{endpoint}")
                elif method.upper() == "POST":
                    response = await self.client.post(f"{self.base_url}{endpoint}", json=body)
                else:
                    raise ValueError(f"Unsupported method: {method}")
                
                req_end = time.time()
                response_times.append((req_end - req_start) * 1000)  # 转换为毫秒
                
                request_count += 1
                if 200 <= response.status_code < 300:
                    successful_requests += 1
                else:
                    failed_requests += 1
                    
            except Exception as e:
                request_count += 1
                failed_requests += 1
                print(f"请求失败: {e}")
        
        # 主测试循环
        while time.time() - start_time < duration:
            # 控制并发数
            if len(tasks) < concurrency:
                task = asyncio.create_task(make_request())
                tasks.add(task)
            else:
                # 等待一些任务完成
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED, timeout=1.0)
                tasks = pending
        
        # 等待所有剩余任务完成
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        
        # 计算统计数据
        elapsed = time.time() - start_time
        rps = request_count / elapsed if elapsed > 0 else 0
        error_rate = (failed_requests / request_count * 100) if request_count > 0 else 0
        
        # 响应时间统计
        if response_times:
            avg_response_time = statistics.mean(response_times)
            median_response_time = statistics.median(response_times)
            p95_response_time = statistics.quantiles(response_times, n=100)[94] if len(response_times) >= 100 else max(response_times)
            p99_response_time = statistics.quantiles(response_times, n=100)[98] if len(response_times) >= 100 else max(response_times)
        else:
            avg_response_time = median_response_time = p95_response_time = p99_response_time = 0
        
        result = BenchmarkResult(
            endpoint=endpoint,
            total_requests=request_count,
            successful_requests=successful_requests,
            failed_requests=failed_requests,
            total_time=elapsed,
            rps=rps,
            avg_response_time=avg_response_time,
            median_response_time=median_response_time,
            p95_response_time=p95_response_time,
            p99_response_time=p99_response_time,
            error_rate=error_rate
        )
        
        self.results.append(result)
        return result
    
    def print_results(self):
        """打印测试结果"""
        print("\n" + "="*100)
        print("性能基准测试结果")
        print("="*100)
        print(f"{'端点':<30} {'请求数':<8} {'成功率':<8} {'RPS':<8} {'平均响应(ms)':<12} {'P95响应(ms)':<12} {'P99响应(ms)':<12}")
        print("-"*100)
        
        for result in self.results:
            print(f"{result.endpoint:<30} {result.total_requests:<8} "
                  f"{(100-result.error_rate):<7.1f}% {result.rps:<7.1f} "
                  f"{result.avg_response_time:<11.1f} {result.p95_response_time:<11.1f} "
                  f"{result.p99_response_time:<11.1f}")
        
        print("="*100)
    
    async def run_standard_benchmark(self):
        """运行标准基准测试套件"""
        print("开始运行 Hemall 标准性能基准测试...")
        
        # 健康检查端点
        await self.benchmark_endpoint("/health/live", concurrency=10, duration=10)
        await self.benchmark_endpoint("/health/ready", concurrency=10, duration=10)
        
        # 核心业务端点 (模拟数据)
        await self.benchmark_endpoint("/products/123", concurrency=50, duration=30)
        await self.benchmark_endpoint("/search/products?q=phone", concurrency=30, duration=30)
        await self.benchmark_endpoint("/inventory/stock/P001", concurrency=100, duration=30)
        
        # 推荐系统端点
        await self.benchmark_endpoint("/recommend/home?user_id=test123", concurrency=25, duration=30)
        await self.benchmark_endpoint("/recommend/hot", concurrency=25, duration=30)
        
        # 订单相关端点
        await self.benchmark_endpoint("/orders/stats", concurrency=20, duration=30)
        
        self.print_results()
    
    async def cleanup(self):
        """清理资源"""
        await self.client.aclose()


async def main():
    """主函数"""
    benchmark = HemallBenchmark("http://localhost:8000")
    
    try:
        await benchmark.run_standard_benchmark()
    finally:
        await benchmark.cleanup()


if __name__ == "__main__":
    asyncio.run(main())