from prometheus_client import start_http_server, Gauge
import psutil
import time

cpu_usage = Gauge('cpu_usage', 'CPU Usage in percentage')
memory_usage = Gauge('memory_usage', 'Memory Usage in percentage')
disk_usage = Gauge('disk_usage', 'Disk Usage in percentage', 'partition')

def collect_system_metrics():
    cpu_usage.set(psutil.cpu_percent())
    memory_usage.set(psutil.virtual_memory().percent)
    for partition in psutil.disk_partitions(all=False):
        usage = psutil.disk_usage(partition.mountpoint)
        disk_usage.labels(partition=partition.mountpoint).set(usage.percent)

if __name__ == '__main__':
    start_http_server(8000)
    while True:
        collect_system_metrics()
        time.sleep(10)