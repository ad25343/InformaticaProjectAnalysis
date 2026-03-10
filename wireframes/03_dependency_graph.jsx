import { useState } from "react";
import { Info, ZoomIn, ZoomOut, Maximize2, ChevronRight } from "lucide-react";

// Simulated DAG data based on FirstBank estate
const stages = [
  {
    label: "Stage 1 — Source Extract",
    color: "bg-blue-50 border-blue-200",
    textColor: "text-blue-700",
    nodes: [
      { id: "stg_cust", name: "m_stg_customer_extract", group: "Staging Extract", short: "STG Customer" },
      { id: "stg_acct", name: "m_stg_account_extract", group: "Staging Extract", short: "STG Account" },
      { id: "stg_txn", name: "m_stg_transaction_extract", group: "Staging Extract", short: "STG Transaction" },
    ],
  },
  {
    label: "Stage 2 — Dimension Load",
    color: "bg-purple-50 border-purple-200",
    textColor: "text-purple-700",
    nodes: [
      { id: "dim_cust", name: "m_dim_customer_scd2", group: "SCD2 Dimension", short: "DIM Customer SCD2", deps: ["stg_cust"] },
      { id: "dim_acct", name: "m_dim_account_scd2", group: "SCD2 Dimension", short: "DIM Account SCD2", deps: ["stg_acct"] },
      { id: "dim_prod", name: "m_dim_product_scd2", group: "SCD2 Dimension", short: "DIM Product SCD2" },
      { id: "dim_branch", name: "m_dim_branch_load", group: "Simple Dimension Load", short: "DIM Branch" },
      { id: "dim_date", name: "m_dim_date_load", group: "Simple Dimension Load", short: "DIM Date" },
      { id: "dim_channel", name: "m_dim_channel_load", group: "Simple Dimension Load", short: "DIM Channel" },
    ],
  },
  {
    label: "Stage 3 — Fact Load",
    color: "bg-amber-50 border-amber-200",
    textColor: "text-amber-700",
    nodes: [
      { id: "fct_daily", name: "m_fct_daily_transactions", group: "Fact + Multi Lookup", short: "FCT Daily Txn", deps: ["stg_txn", "dim_cust", "dim_acct", "dim_date", "dim_channel"] },
      { id: "fct_loan", name: "m_fct_loan_origination", group: "Fact + Single Lookup", short: "FCT Loan Orig" },
      { id: "fct_card", name: "m_fct_card_transactions", group: "Fact + Single Lookup", short: "FCT Card Txn", deps: ["dim_acct"] },
      { id: "fct_wire", name: "m_fct_wire_transfers", group: "Fact + Single Lookup", short: "FCT Wire Xfer" },
      { id: "fct_fraud", name: "m_fct_fraud_detection", group: "Complex Risk", short: "FCT Fraud", deps: ["stg_txn"] },
      { id: "fct_aml", name: "m_fct_aml_monitoring", group: "Complex Risk", short: "FCT AML", deps: ["dim_cust"] },
    ],
  },
  {
    label: "Stage 4 — Aggregation",
    color: "bg-green-50 border-green-200",
    textColor: "text-green-700",
    nodes: [
      { id: "agg_monthly", name: "m_agg_monthly_customer_summary", group: "Aggregation", short: "AGG Monthly Cust", deps: ["fct_daily"] },
      { id: "agg_branch", name: "m_agg_branch_daily_totals", group: "Aggregation", short: "AGG Branch Daily", deps: ["fct_daily"] },
      { id: "agg_product", name: "m_agg_product_performance", group: "Aggregation", short: "AGG Product Perf", deps: ["fct_daily"] },
      { id: "dim_seg", name: "m_dim_customer_segment", group: "Unique", short: "DIM Cust Segment", deps: ["dim_cust", "agg_monthly"] },
    ],
  },
];

const groupColors = {
  "Staging Extract": { bg: "bg-blue-100", border: "border-blue-300", text: "text-blue-800" },
  "SCD2 Dimension": { bg: "bg-purple-100", border: "border-purple-300", text: "text-purple-800" },
  "Simple Dimension Load": { bg: "bg-indigo-100", border: "border-indigo-300", text: "text-indigo-800" },
  "Fact + Multi Lookup": { bg: "bg-amber-100", border: "border-amber-300", text: "text-amber-800" },
  "Fact + Single Lookup": { bg: "bg-orange-100", border: "border-orange-300", text: "text-orange-800" },
  "Complex Risk": { bg: "bg-red-100", border: "border-red-300", text: "text-red-800" },
  "Aggregation": { bg: "bg-green-100", border: "border-green-300", text: "text-green-800" },
  "Unique": { bg: "bg-gray-100", border: "border-gray-300", text: "text-gray-700" },
};

const NodeCard = ({ node, selected, onClick }) => {
  const colors = groupColors[node.group] || groupColors["Unique"];
  return (
    <button
      onClick={() => onClick(node)}
      className={`px-3 py-2 rounded-lg border text-left transition-all ${colors.bg} ${colors.border} ${
        selected ? "ring-2 ring-blue-500 shadow-md" : "hover:shadow-sm"
      }`}
    >
      <p className={`text-xs font-semibold ${colors.text}`}>{node.short}</p>
      <p className="text-xs text-gray-500 mt-0.5">{node.group}</p>
    </button>
  );
};

const EdgeIndicator = ({ from, to }) => (
  <div className="text-xs text-gray-400 flex items-center gap-1">
    <span className="font-mono">{from}</span>
    <ChevronRight className="w-3 h-3" />
    <span className="font-mono">{to}</span>
  </div>
);

export default function DependencyGraph() {
  const [selectedNode, setSelectedNode] = useState(null);
  const [highlightDeps, setHighlightDeps] = useState(false);

  const handleNodeClick = (node) => {
    setSelectedNode(selectedNode?.id === node.id ? null : node);
  };

  // Find all nodes that depend on the selected node (downstream)
  const getDownstream = (nodeId) => {
    const downstream = [];
    stages.forEach(stage => {
      stage.nodes.forEach(n => {
        if (n.deps && n.deps.includes(nodeId)) downstream.push(n.id);
      });
    });
    return downstream;
  };

  // Find all nodes the selected node depends on (upstream)
  const getUpstream = (nodeId) => {
    const node = stages.flatMap(s => s.nodes).find(n => n.id === nodeId);
    return node?.deps || [];
  };

  const downstream = selectedNode ? getDownstream(selectedNode.id) : [];
  const upstream = selectedNode ? getUpstream(selectedNode.id) : [];

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b border-gray-200 px-6 py-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold text-gray-900">InformaticaProjectAnalysis</h1>
            <p className="text-sm text-gray-500 mt-0.5">FirstBank DWH Migration · Dependency Graph</p>
          </div>
          <div className="flex items-center gap-2">
            <button className="p-1.5 text-gray-400 hover:text-gray-600 border border-gray-200 rounded">
              <ZoomIn className="w-4 h-4" />
            </button>
            <button className="p-1.5 text-gray-400 hover:text-gray-600 border border-gray-200 rounded">
              <ZoomOut className="w-4 h-4" />
            </button>
            <button className="p-1.5 text-gray-400 hover:text-gray-600 border border-gray-200 rounded">
              <Maximize2 className="w-4 h-4" />
            </button>
          </div>
        </div>
        <div className="flex gap-6 mt-4 border-b border-gray-100 -mb-4">
          <button className="pb-3 text-sm text-gray-500 hover:text-gray-700">Dashboard</button>
          <button className="pb-3 text-sm text-gray-500 hover:text-gray-700">Pattern Groups</button>
          <button className="pb-3 text-sm font-medium text-blue-600 border-b-2 border-blue-600">Dependency Graph</button>
        </div>
      </header>

      <div className="flex h-[calc(100vh-105px)]">
        {/* Main graph area */}
        <div className="flex-1 overflow-auto p-6">
          {/* Legend */}
          <div className="flex flex-wrap gap-3 mb-6">
            {Object.entries(groupColors).map(([name, colors]) => (
              <div key={name} className="flex items-center gap-1.5">
                <div className={`w-3 h-3 rounded ${colors.bg} ${colors.border} border`} />
                <span className="text-xs text-gray-500">{name}</span>
              </div>
            ))}
          </div>

          {/* DAG visualization (staged layout) */}
          <div className="space-y-6">
            {stages.map((stage, si) => (
              <div key={si}>
                {/* Stage label */}
                <div className={`inline-block px-3 py-1 rounded-full text-xs font-medium mb-3 border ${stage.color} ${stage.textColor}`}>
                  {stage.label}
                </div>

                {/* Stage nodes */}
                <div className="flex flex-wrap gap-3 ml-4">
                  {stage.nodes.map((node) => {
                    const isSelected = selectedNode?.id === node.id;
                    const isUpstream = upstream.includes(node.id);
                    const isDownstream = downstream.includes(node.id);
                    const dimmed = selectedNode && !isSelected && !isUpstream && !isDownstream;

                    return (
                      <div key={node.id} className={`transition-opacity ${dimmed ? "opacity-30" : "opacity-100"}`}>
                        <NodeCard
                          node={node}
                          selected={isSelected}
                          onClick={handleNodeClick}
                        />
                        {/* Dependency arrows (text-based for wireframe) */}
                        {node.deps && isSelected && (
                          <div className="mt-1 ml-1 space-y-0.5">
                            {node.deps.map((dep, di) => {
                              const depNode = stages.flatMap(s => s.nodes).find(n => n.id === dep);
                              return depNode ? (
                                <div key={di} className="text-xs text-blue-500">
                                  ↑ depends on {depNode.short}
                                </div>
                              ) : null;
                            })}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>

                {/* Stage separator */}
                {si < stages.length - 1 && (
                  <div className="flex items-center gap-2 ml-4 mt-3">
                    <div className="flex-1 border-t border-dashed border-gray-200" />
                    <span className="text-xs text-gray-300">depends on above</span>
                    <div className="flex-1 border-t border-dashed border-gray-200" />
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>

        {/* Right panel — node detail */}
        <div className="w-80 bg-white border-l border-gray-200 overflow-y-auto flex-shrink-0">
          {selectedNode ? (
            <div className="p-4">
              <h3 className="text-sm font-semibold text-gray-900 mb-1">{selectedNode.name}</h3>
              <span className={`text-xs px-2 py-0.5 rounded ${groupColors[selectedNode.group].bg} ${groupColors[selectedNode.group].text}`}>
                {selectedNode.group}
              </span>

              {/* Upstream dependencies */}
              <div className="mt-4">
                <p className="text-xs text-gray-500 uppercase tracking-wide mb-2">Depends On ({upstream.length})</p>
                {upstream.length === 0 ? (
                  <p className="text-xs text-gray-400">No upstream dependencies — root node</p>
                ) : (
                  <div className="space-y-1.5">
                    {upstream.map(depId => {
                      const depNode = stages.flatMap(s => s.nodes).find(n => n.id === depId);
                      return depNode ? (
                        <button
                          key={depId}
                          onClick={() => handleNodeClick(depNode)}
                          className="w-full text-left px-2 py-1.5 text-xs bg-blue-50 rounded border border-blue-100 hover:bg-blue-100"
                        >
                          <span className="font-medium text-blue-700">{depNode.short}</span>
                          <span className="text-blue-400 ml-1">({depNode.group})</span>
                        </button>
                      ) : null;
                    })}
                  </div>
                )}
              </div>

              {/* Downstream dependents */}
              <div className="mt-4">
                <p className="text-xs text-gray-500 uppercase tracking-wide mb-2">Depended On By ({downstream.length})</p>
                {downstream.length === 0 ? (
                  <p className="text-xs text-gray-400">No downstream dependents — leaf node</p>
                ) : (
                  <div className="space-y-1.5">
                    {downstream.map(depId => {
                      const depNode = stages.flatMap(s => s.nodes).find(n => n.id === depId);
                      return depNode ? (
                        <button
                          key={depId}
                          onClick={() => handleNodeClick(depNode)}
                          className="w-full text-left px-2 py-1.5 text-xs bg-amber-50 rounded border border-amber-100 hover:bg-amber-100"
                        >
                          <span className="font-medium text-amber-700">{depNode.short}</span>
                          <span className="text-amber-400 ml-1">({depNode.group})</span>
                        </button>
                      ) : null;
                    })}
                  </div>
                )}
              </div>

              {/* Impact analysis */}
              <div className="mt-4 p-3 bg-red-50 rounded-lg border border-red-100">
                <div className="flex items-center gap-1.5 mb-1">
                  <Info className="w-3.5 h-3.5 text-red-500" />
                  <p className="text-xs font-medium text-red-700">Error Propagation</p>
                </div>
                <p className="text-xs text-red-600">
                  If <span className="font-mono font-medium">{selectedNode.short}</span> fails,
                  {downstream.length > 0
                    ? ` ${downstream.length} downstream mapping${downstream.length > 1 ? "s" : ""} will be affected.`
                    : " no downstream mappings are affected (leaf node)."}
                </p>
              </div>

              {/* Execution info */}
              <div className="mt-4">
                <p className="text-xs text-gray-500 uppercase tracking-wide mb-2">Execution</p>
                <div className="space-y-1.5 text-xs">
                  <div className="flex justify-between">
                    <span className="text-gray-500">Stage</span>
                    <span className="text-gray-700 font-medium">
                      {stages.findIndex(s => s.nodes.some(n => n.id === selectedNode.id)) + 1} of {stages.length}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-500">Can run parallel</span>
                    <span className="text-gray-700 font-medium">
                      {upstream.length === 0 ? "Yes — no dependencies" : "After upstream completes"}
                    </span>
                  </div>
                </div>
              </div>
            </div>
          ) : (
            <div className="p-4 flex flex-col items-center justify-center h-full text-center">
              <Info className="w-8 h-8 text-gray-300 mb-2" />
              <p className="text-sm text-gray-400">Click a node to see details</p>
              <p className="text-xs text-gray-300 mt-1">Dependencies, downstream impact, and execution info</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
