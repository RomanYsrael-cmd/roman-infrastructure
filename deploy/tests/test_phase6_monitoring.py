import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("phase6_metrics", ROOT / "deploy" / "monitoring" / "collect_romanlms_phase6.py")
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)


class Phase6MonitoringTests(unittest.TestCase):
    def test_aggregates_http_and_jvm_values_without_exporting_uri_labels(self):
        text = '''http_server_requests_seconds_count{method="GET",status="200",uri="/private/synthetic"} 4
http_server_requests_seconds_count{method="POST",status="500",uri="/private/synthetic"} 2
http_server_requests_seconds_sum{method="GET",status="200",uri="/private/synthetic"} 1.5
jvm_memory_used_bytes{area="heap",id="one"} 100
jvm_memory_used_bytes{area="heap",id="two"} 200
jvm_memory_used_bytes{area="nonheap",id="three"} 40
jvm_memory_max_bytes{area="heap",id="one"} 512
process_cpu_usage 0.25
hikaricp_connections_active{pool="db"} 3
'''
        values = metrics.aggregate_metrics(text)
        self.assertEqual(values["http_2xx"], 4)
        self.assertEqual(values["http_5xx"], 2)
        self.assertEqual(values["http_count"], 6)
        self.assertEqual(values["http_duration"], 1.5)
        self.assertEqual(values["heap_used"], 300)
        self.assertEqual(values["nonheap_used"], 40)
        self.assertEqual(values["heap_max"], 512)
        self.assertEqual(values["db_connections"], 3)
        self.assertNotIn("private", repr(values))

    def test_container_statistics_are_limited_to_requested_components(self):
        output = '{"Name":"expected","MemUsage":"2MiB / 1GiB","CPUPerc":"3.5%"}\n{"Name":"unrelated","MemUsage":"3MiB / 1GiB","CPUPerc":"5%"}\n'
        result = subprocess.CompletedProcess([], 0, output, "")
        with mock.patch.object(metrics.subprocess, "run", return_value=result):
            self.assertEqual(metrics.docker_stats({"backend": "expected"}), {"backend": (2 * 1024**2, 3.5)})
