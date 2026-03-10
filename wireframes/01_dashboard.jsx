import { useState } from "react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, PieChart, Pie, Cell } from "recharts";
import { CheckCircle, AlertTriangle, XCircle, ChevronRight, FileText, Download, Activity } from "lucide-react";

const complexityData = [
  { tier: "Simple", count: 15, fill: "#22c55e" },
  { tier: "Medium", count: 20, fill: "#f59e0b" },
  { tier: "Complex", count: 15, fill: "#ef4444" },
];

const confidenceData = [
  { name: "HIGH", value: 35, color: "#22c55e" },
  { name: "MEDIUM", value: 10, color: "#f59e0b" },
  { name: "LOW", value: 3, color: "#ef4444" },
  { name: "UNCLASSIFIED", value: 2, color: "#94a3b8" },
];

const patternGroups = [
  { name: "Simple Dimension Load", count: 7, spine: "SQ → EXP → TGT", confidence: "HIGH" },
  { name: "Reference Table Load", count: 4, spine: "SQ → EXP → TGT", confidence: "HIGH" },
  { name: "Staging Extract", count: 3, spine: "SQ → FIL → TGT", confidence: "HIGH" },
  { name: "SCD2 Dimension", count: 3, spine: "SQ → LKP → EXP → RTR → UPD", confidence: "HIGH" },
  { name: "Fact + Single Lookup", count: 7, spine: "SQ → LKP → EXP → TGT", confidence: "MEDIUM" },
  { name: "Aggregation", count: 3, spine: "SQ → [JNR] → AGG → TGT", confidence: "HIGH" },
  { name: "Complex Risk/Regulatory", count: 4, spine: "SQ(×3) → JNR → LKP(×2) → EXP → RTR", confidence: "MEDIUM" },
  { name: "Multi-Source Fact", count: 5, spine: "SQ(×3) → JNR(×2) → LKP → EXP → TGT", confidence: "MEDIUM" },
];

const riskFlags = [
  { severity: "HIGH", count: 4, desc: "Custom SQL overrides, missing mapplet definitions" },
  { severity: "MEDIUM", count: 8, desc: "Tier 2 variation, expression complexity uncertain" },
  { severity: "LOW", count: 6, desc: "Minor naming inconsistencies, optional parameters" },
];

const StatusBadge = ({ status }) => {
  const styles = {
    HIGH: "bg-green-100 text-green-800 border-green-200",
    MEDIUM: "bg-yellow-100 text-yellow-800 border-yellow-200",
    LOW: "bg-red-100 text-red-800 border-red-200",
  };
  return (
    <span className={`px-2 py-0.5 rounded-full text-xs font-medium border ${styles[status]}`}>
      {status}
    </span>
  );
};

export default function Dashboard() {
  const [reviewState, setReviewState] = useState("pending");

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b border-gray-200 px-6 py-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold text-gray-900">InformaticaProjectAnalysis</h1>
            <p className="text-sm text-gray-500 mt-0.5">Estate-level pre-conversion analysis</p>
          </div>
          <div className="flex items-center gap-3">
            <span className="text-xs text-gray-400">v0.1.0</span>
            <div className="w-2 h-2 rounded-full bg-green-400" title="Connected" />
          </div>
        </div>
        {/* Navigation tabs */}
        <div className="flex gap-6 mt-4 border-b border-gray-100 -mb-4">
          <button className="pb-3 text-sm font-medium text-blue-600 border-b-2 border-blue-600">Dashboard</button>
          <button className="pb-3 text-sm text-gray-500 hover:text-gray-700">Pattern Groups</button>
          <button className="pb-3 text-sm text-gray-500 hover:text-gray-700">Dependency Graph</button>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-6 py-6">
        {/* Project header */}
        <div className="bg-white rounded-lg border border-gray-200 p-5 mb-6">
          <div className="flex items-start justify-between">
            <div>
              <h2 className="text-lg font-semibold text-gray-900">FirstBank DWH Migration</h2>
              <p className="text-sm text-gray-500 mt-1">Analysis completed · March 9, 2026 · 50 mappings analyzed</p>
            </div>
            <div className="flex gap-2">
              <button className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-600 bg-white border border-gray-200 rounded-md hover:bg-gray-50">
                <Download className="w-3.5 h-3.5" /> PDF
              </button>
              <button className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-600 bg-white border border-gray-200 rounded-md hover:bg-gray-50">
                <Download className="w-3.5 h-3.5" /> Excel
              </button>
              <button className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-600 bg-white border border-gray-200 rounded-md hover:bg-gray-50">
                <FileText className="w-3.5 h-3.5" /> Strategy JSON
              </button>
            </div>
          </div>
        </div>

        {/* KPI cards */}
        <div className="grid grid-cols-5 gap-4 mb-6">
          <div className="bg-white rounded-lg border border-gray-200 p-4">
            <p className="text-xs text-gray-500 uppercase tracking-wide">Total Mappings</p>
            <p className="text-2xl font-bold text-gray-900 mt-1">50</p>
          </div>
          <div className="bg-white rounded-lg border border-gray-200 p-4">
            <p className="text-xs text-gray-500 uppercase tracking-wide">Pattern Groups</p>
            <p className="text-2xl font-bold text-blue-600 mt-1">8</p>
          </div>
          <div className="bg-white rounded-lg border border-gray-200 p-4">
            <p className="text-xs text-gray-500 uppercase tracking-wide">Template Candidates</p>
            <p className="text-2xl font-bold text-green-600 mt-1">36</p>
            <p className="text-xs text-gray-400 mt-0.5">across 8 groups</p>
          </div>
          <div className="bg-white rounded-lg border border-gray-200 p-4">
            <p className="text-xs text-gray-500 uppercase tracking-wide">Unique Mappings</p>
            <p className="text-2xl font-bold text-amber-600 mt-1">14</p>
            <p className="text-xs text-gray-400 mt-0.5">individual conversion</p>
          </div>
          <div className="bg-white rounded-lg border border-gray-200 p-4 bg-green-50 border-green-200">
            <p className="text-xs text-green-700 uppercase tracking-wide">Scope Reduction</p>
            <p className="text-2xl font-bold text-green-700 mt-1">56%</p>
            <p className="text-xs text-green-600 mt-0.5">50 → 22 outputs</p>
          </div>
        </div>

        <div className="grid grid-cols-3 gap-6 mb-6">
          {/* Complexity distribution */}
          <div className="bg-white rounded-lg border border-gray-200 p-5">
            <h3 className="text-sm font-medium text-gray-700 mb-4">Complexity Distribution</h3>
            <ResponsiveContainer width="100%" height={160}>
              <BarChart data={complexityData} barSize={40}>
                <XAxis dataKey="tier" tick={{ fontSize: 12 }} />
                <YAxis tick={{ fontSize: 12 }} />
                <Tooltip />
                <Bar dataKey="count" radius={[4, 4, 0, 0]}>
                  {complexityData.map((entry, i) => (
                    <Cell key={i} fill={entry.fill} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>

          {/* Confidence distribution */}
          <div className="bg-white rounded-lg border border-gray-200 p-5">
            <h3 className="text-sm font-medium text-gray-700 mb-4">Confidence Distribution</h3>
            <div className="flex items-center justify-center">
              <ResponsiveContainer width="100%" height={160}>
                <PieChart>
                  <Pie data={confidenceData} cx="50%" cy="50%" innerRadius={40} outerRadius={65} dataKey="value" label={({ name, value }) => `${name}: ${value}`}>
                    {confidenceData.map((entry, i) => (
                      <Cell key={i} fill={entry.color} />
                    ))}
                  </Pie>
                  <Tooltip />
                </PieChart>
              </ResponsiveContainer>
            </div>
          </div>

          {/* Dependency depth */}
          <div className="bg-white rounded-lg border border-gray-200 p-5">
            <h3 className="text-sm font-medium text-gray-700 mb-4">Dependency Structure</h3>
            <div className="space-y-3">
              <div className="flex items-center gap-3">
                <div className="w-20 text-xs text-gray-500">Depth</div>
                <div className="text-lg font-semibold text-gray-900">4 layers</div>
              </div>
              <div className="flex items-center gap-2 text-xs text-gray-500">
                <span className="px-2 py-1 bg-blue-50 text-blue-700 rounded">STG</span>
                <ChevronRight className="w-3 h-3" />
                <span className="px-2 py-1 bg-purple-50 text-purple-700 rounded">DIM</span>
                <ChevronRight className="w-3 h-3" />
                <span className="px-2 py-1 bg-amber-50 text-amber-700 rounded">FCT</span>
                <ChevronRight className="w-3 h-3" />
                <span className="px-2 py-1 bg-green-50 text-green-700 rounded">AGG</span>
              </div>
              <div className="mt-2 space-y-1.5">
                <div className="flex justify-between text-xs">
                  <span className="text-gray-500">Cross-mapping dependencies</span>
                  <span className="font-medium text-gray-700">12</span>
                </div>
                <div className="flex justify-between text-xs">
                  <span className="text-gray-500">Shared lookup tables</span>
                  <span className="font-medium text-gray-700">8</span>
                </div>
                <div className="flex justify-between text-xs">
                  <span className="text-gray-500">Parallel execution tracks</span>
                  <span className="font-medium text-gray-700">3</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Pattern groups summary table */}
        <div className="bg-white rounded-lg border border-gray-200 p-5 mb-6">
          <h3 className="text-sm font-medium text-gray-700 mb-4">Pattern Groups</h3>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-gray-500 uppercase tracking-wide border-b border-gray-100">
                <th className="pb-2 font-medium">Group</th>
                <th className="pb-2 font-medium">Spine</th>
                <th className="pb-2 font-medium text-center">Mappings</th>
                <th className="pb-2 font-medium text-center">Confidence</th>
                <th className="pb-2 font-medium text-right">Output</th>
              </tr>
            </thead>
            <tbody>
              {patternGroups.map((g, i) => (
                <tr key={i} className="border-b border-gray-50 hover:bg-gray-50 cursor-pointer">
                  <td className="py-2.5 font-medium text-gray-900">{g.name}</td>
                  <td className="py-2.5 text-gray-500 font-mono text-xs">{g.spine}</td>
                  <td className="py-2.5 text-center text-gray-700">{g.count}</td>
                  <td className="py-2.5 text-center"><StatusBadge status={g.confidence} /></td>
                  <td className="py-2.5 text-right text-xs text-gray-500">1 template + config</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Risk flags */}
        <div className="bg-white rounded-lg border border-gray-200 p-5 mb-6">
          <h3 className="text-sm font-medium text-gray-700 mb-4">Risk Flags</h3>
          <div className="space-y-2">
            {riskFlags.map((f, i) => (
              <div key={i} className="flex items-center gap-3 text-sm">
                {f.severity === "HIGH" && <XCircle className="w-4 h-4 text-red-500 flex-shrink-0" />}
                {f.severity === "MEDIUM" && <AlertTriangle className="w-4 h-4 text-amber-500 flex-shrink-0" />}
                {f.severity === "LOW" && <Activity className="w-4 h-4 text-blue-400 flex-shrink-0" />}
                <span className="text-gray-500 w-16 flex-shrink-0">{f.severity}</span>
                <span className="text-gray-700">{f.count} mappings — {f.desc}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Review gate */}
        <div className="bg-white rounded-lg border-2 border-blue-200 p-6">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="text-sm font-semibold text-gray-900">Strategy Review Gate</h3>
              <p className="text-xs text-gray-500 mt-1">
                {reviewState === "pending"
                  ? "Review the pattern groups and dependency graph before approving."
                  : reviewState === "approved"
                  ? "Strategy approved. Ready for download."
                  : "Strategy rejected. Analysis will re-run with reviewer notes."}
              </p>
            </div>
            {reviewState === "pending" && (
              <div className="flex gap-2">
                <button
                  onClick={() => setReviewState("rejected")}
                  className="px-4 py-2 text-sm font-medium text-red-600 bg-white border border-red-200 rounded-md hover:bg-red-50"
                >
                  Reject
                </button>
                <button
                  onClick={() => setReviewState("approved")}
                  className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-md hover:bg-blue-700"
                >
                  Approve Strategy
                </button>
              </div>
            )}
            {reviewState === "approved" && (
              <div className="flex items-center gap-2 text-green-600">
                <CheckCircle className="w-5 h-5" />
                <span className="text-sm font-medium">Approved</span>
              </div>
            )}
            {reviewState === "rejected" && (
              <button
                onClick={() => setReviewState("pending")}
                className="px-4 py-2 text-sm font-medium text-gray-600 bg-white border border-gray-200 rounded-md hover:bg-gray-50"
              >
                Reset
              </button>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}
