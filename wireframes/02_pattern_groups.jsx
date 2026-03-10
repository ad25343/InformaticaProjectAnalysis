import { useState } from "react";
import { ChevronRight, Check, ArrowRight, AlertTriangle, MessageSquare } from "lucide-react";

const groups = [
  {
    id: "dim_load",
    name: "Simple Dimension Load",
    count: 7,
    confidence: "HIGH",
    spine: ["SQ", "EXP", "TARGET"],
    description: "Single-source dimension loads with basic expression transformations. All members share identical topology — only table names, column lists, and expression details differ.",
    members: [
      { name: "m_dim_customer_load", tier: 1, confidence: "HIGH", source: "CUSTOMER", target: "DIM_CUSTOMER", fields: 14, flags: [] },
      { name: "m_dim_branch_load", tier: 1, confidence: "HIGH", source: "BRANCH", target: "DIM_BRANCH", fields: 12, flags: [] },
      { name: "m_dim_product_load", tier: 1, confidence: "HIGH", source: "PRODUCT", target: "DIM_PRODUCT", fields: 9, flags: [] },
      { name: "m_dim_date_load", tier: 1, confidence: "HIGH", source: "DATE_STAGING", target: "DIM_DATE", fields: 11, flags: [] },
      { name: "m_dim_currency_load", tier: 1, confidence: "HIGH", source: "CURRENCY", target: "DIM_CURRENCY", fields: 6, flags: [] },
      { name: "m_dim_employee_load", tier: 1, confidence: "HIGH", source: "EMPLOYEE", target: "DIM_EMPLOYEE", fields: 11, flags: [] },
      { name: "m_dim_channel_load", tier: 1, confidence: "HIGH", source: "CHANNEL", target: "DIM_CHANNEL", fields: 6, flags: [] },
    ],
    params: ["source_table", "target_table", "column_list", "expression_logic"],
    paramDiffs: [
      { param: "source_table", values: ["CUSTOMER", "BRANCH", "PRODUCT", "DATE_STAGING", "CURRENCY", "EMPLOYEE", "CHANNEL"] },
      { param: "target_table", values: ["DIM_CUSTOMER", "DIM_BRANCH", "DIM_PRODUCT", "DIM_DATE", "DIM_CURRENCY", "DIM_EMPLOYEE", "DIM_CHANNEL"] },
      { param: "field_count", values: ["14", "12", "9", "11", "6", "11", "6"] },
    ],
  },
  {
    id: "ref_load",
    name: "Reference Table Load",
    count: 4,
    confidence: "HIGH",
    spine: ["SQ", "EXP", "TARGET"],
    description: "Reference/lookup table loads. Same spine as dimension loads but different business domain. Expression complexity varies slightly — m_ref_interest_rates has a calculation (FINAL_RATE = BASE + SPREAD).",
    members: [
      { name: "m_ref_interest_rates", tier: 2, confidence: "MEDIUM", source: "INTEREST_RATE_TABLE", target: "REF_INTEREST_RATES", fields: 7, flags: ["Expression has calculation logic"] },
      { name: "m_ref_fee_schedule", tier: 1, confidence: "HIGH", source: "FEE_SCHEDULE", target: "REF_FEE_SCHEDULE", fields: 8, flags: [] },
      { name: "m_ref_country_codes", tier: 1, confidence: "HIGH", source: "COUNTRY_REF", target: "REF_COUNTRY_CODES", fields: 6, flags: [] },
      { name: "m_ref_exchange_rates", tier: 1, confidence: "HIGH", source: "FX_RATE_DAILY", target: "REF_EXCHANGE_RATES", fields: 6, flags: [] },
    ],
    params: ["source_table", "target_table", "column_list"],
    paramDiffs: [
      { param: "source_table", values: ["INTEREST_RATE_TABLE", "FEE_SCHEDULE", "COUNTRY_REF", "FX_RATE_DAILY"] },
      { param: "target_table", values: ["REF_INTEREST_RATES", "REF_FEE_SCHEDULE", "REF_COUNTRY_CODES", "REF_EXCHANGE_RATES"] },
    ],
  },
  {
    id: "stg_extract",
    name: "Staging Extract",
    count: 3,
    confidence: "HIGH",
    spine: ["SQ", "FIL", "EXP", "TARGET"],
    description: "Staging extractions with a Filter transformation. The filter condition varies per mapping (active records, non-archived, previous day).",
    members: [
      { name: "m_stg_customer_extract", tier: 2, confidence: "HIGH", source: "CRM_CUSTOMERS", target: "STG_CUSTOMER", fields: 7, flags: [] },
      { name: "m_stg_account_extract", tier: 2, confidence: "HIGH", source: "CORE_ACCOUNTS", target: "STG_ACCOUNT", fields: 6, flags: [] },
      { name: "m_stg_transaction_extract", tier: 2, confidence: "HIGH", source: "TRANSACTIONS_RAW", target: "STG_TRANSACTION", fields: 5, flags: [] },
    ],
    params: ["source_table", "target_table", "filter_condition"],
    paramDiffs: [
      { param: "source_table", values: ["CRM_CUSTOMERS", "CORE_ACCOUNTS", "TRANSACTIONS_RAW"] },
      { param: "filter_condition", values: ["active_flag = 'Y'", "status != 'ARCHIVED'", "txn_date = TRUNC(SYSDATE) - 1"] },
    ],
  },
  {
    id: "scd2",
    name: "SCD2 Dimension",
    count: 3,
    confidence: "HIGH",
    spine: ["SQ", "LKP", "EXP", "RTR", "UPD", "TARGET"],
    description: "Slowly Changing Dimension Type 2 pattern. Self-lookup against existing dimension, change detection in Expression, Router splits new/changed/unchanged, Update Strategy applies insert/update.",
    members: [
      { name: "m_dim_customer_scd2", tier: 1, confidence: "HIGH", source: "STG_CUSTOMER", target: "DIM_CUSTOMER", fields: 14, flags: [] },
      { name: "m_dim_account_scd2", tier: 1, confidence: "HIGH", source: "STG_ACCOUNT", target: "DIM_ACCOUNT", fields: 12, flags: [] },
      { name: "m_dim_product_scd2", tier: 2, confidence: "MEDIUM", source: "PRODUCT", target: "DIM_PRODUCT", fields: 9, flags: ["No UPD in spine — verify"] },
    ],
    params: ["source_table", "target_table", "natural_key_columns", "tracked_columns"],
    paramDiffs: [
      { param: "natural_key", values: ["CUSTOMER_ID", "ACCOUNT_ID", "PRODUCT_ID"] },
      { param: "tracked_columns", values: ["address, phone, email, status", "balance_type, interest_rate, status", "product_name, category, rate"] },
    ],
  },
  { id: "fact_lkp", name: "Fact + Single Lookup", count: 7, confidence: "MEDIUM", spine: ["SQ", "LKP", "EXP", "TARGET"], description: "Fact table loads with a single lookup join.", members: [], params: [], paramDiffs: [] },
  { id: "agg", name: "Aggregation", count: 3, confidence: "HIGH", spine: ["SQ", "JNR", "AGG", "TARGET"], description: "Aggregation patterns with GROUP BY.", members: [], params: [], paramDiffs: [] },
  { id: "complex_risk", name: "Complex Risk/Regulatory", count: 4, confidence: "MEDIUM", spine: ["SQ(×3)", "JNR", "LKP(×2)", "EXP", "RTR"], description: "Multi-source regulatory and risk calculations.", members: [], params: [], paramDiffs: [] },
  { id: "multi_src", name: "Multi-Source Fact", count: 5, confidence: "MEDIUM", spine: ["SQ(×3)", "JNR(×2)", "LKP", "EXP", "TARGET"], description: "Complex fact loads with multiple source joins.", members: [], params: [], paramDiffs: [] },
];

const tierColors = { 1: "text-green-600 bg-green-50", 2: "text-amber-600 bg-amber-50", 3: "text-red-600 bg-red-50" };
const confColors = { HIGH: "bg-green-100 text-green-800", MEDIUM: "bg-yellow-100 text-yellow-800", LOW: "bg-red-100 text-red-800" };

const SpineViz = ({ spine }) => (
  <div className="flex items-center gap-1.5 flex-wrap">
    {spine.map((step, i) => (
      <div key={i} className="flex items-center gap-1.5">
        <span className="px-2.5 py-1 bg-blue-50 text-blue-700 text-xs font-mono rounded border border-blue-100">
          {step}
        </span>
        {i < spine.length - 1 && <ArrowRight className="w-3.5 h-3.5 text-gray-300" />}
      </div>
    ))}
  </div>
);

export default function PatternGroups() {
  const [selectedGroup, setSelectedGroup] = useState(groups[0]);
  const [overrides, setOverrides] = useState({});
  const [notes, setNotes] = useState({});
  const [showNotes, setShowNotes] = useState(null);

  const setOverride = (mappingName, action) => {
    setOverrides(prev => ({ ...prev, [mappingName]: action }));
  };

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b border-gray-200 px-6 py-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold text-gray-900">InformaticaProjectAnalysis</h1>
            <p className="text-sm text-gray-500 mt-0.5">FirstBank DWH Migration · 50 mappings · 8 pattern groups</p>
          </div>
        </div>
        <div className="flex gap-6 mt-4 border-b border-gray-100 -mb-4">
          <button className="pb-3 text-sm text-gray-500 hover:text-gray-700">Dashboard</button>
          <button className="pb-3 text-sm font-medium text-blue-600 border-b-2 border-blue-600">Pattern Groups</button>
          <button className="pb-3 text-sm text-gray-500 hover:text-gray-700">Dependency Graph</button>
        </div>
      </header>

      <div className="flex h-[calc(100vh-105px)]">
        {/* Left panel — group list */}
        <div className="w-80 bg-white border-r border-gray-200 overflow-y-auto flex-shrink-0">
          <div className="p-3">
            <p className="text-xs text-gray-500 uppercase tracking-wide px-2 mb-2">Pattern Groups ({groups.length})</p>
            {groups.map((g) => (
              <button
                key={g.id}
                onClick={() => setSelectedGroup(g)}
                className={`w-full text-left px-3 py-2.5 rounded-md mb-1 transition-colors ${
                  selectedGroup.id === g.id ? "bg-blue-50 border border-blue-200" : "hover:bg-gray-50 border border-transparent"
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium text-gray-900 truncate">{g.name}</span>
                  <span className={`text-xs px-1.5 py-0.5 rounded ${confColors[g.confidence]}`}>{g.count}</span>
                </div>
                <div className="flex items-center gap-1 mt-1 text-xs text-gray-400 font-mono truncate">
                  {g.spine.join(" → ")}
                </div>
              </button>
            ))}

            <div className="border-t border-gray-100 mt-3 pt-3 px-2">
              <p className="text-xs text-gray-500 uppercase tracking-wide mb-2">Unique Mappings (14)</p>
              <p className="text-xs text-gray-400">Individual conversion — no template</p>
            </div>
          </div>
        </div>

        {/* Right panel — group detail */}
        <div className="flex-1 overflow-y-auto p-6">
          {/* Group header */}
          <div className="mb-6">
            <div className="flex items-center gap-3 mb-2">
              <h2 className="text-lg font-semibold text-gray-900">{selectedGroup.name}</h2>
              <span className={`text-xs px-2 py-0.5 rounded-full ${confColors[selectedGroup.confidence]}`}>
                {selectedGroup.confidence} confidence
              </span>
            </div>
            <p className="text-sm text-gray-500">{selectedGroup.description}</p>
          </div>

          {/* Spine visualization */}
          <div className="bg-white rounded-lg border border-gray-200 p-4 mb-4">
            <p className="text-xs text-gray-500 uppercase tracking-wide mb-3">Transformation Spine</p>
            <SpineViz spine={selectedGroup.spine} />
          </div>

          {/* Member mappings */}
          {selectedGroup.members.length > 0 && (
            <div className="bg-white rounded-lg border border-gray-200 p-4 mb-4">
              <p className="text-xs text-gray-500 uppercase tracking-wide mb-3">
                Member Mappings ({selectedGroup.members.length})
              </p>
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-gray-500 uppercase tracking-wide border-b border-gray-100">
                    <th className="pb-2 font-medium">Mapping</th>
                    <th className="pb-2 font-medium">Source → Target</th>
                    <th className="pb-2 font-medium text-center">Tier</th>
                    <th className="pb-2 font-medium text-center">Confidence</th>
                    <th className="pb-2 font-medium text-center">Flags</th>
                    <th className="pb-2 font-medium text-right">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {selectedGroup.members.map((m, i) => (
                    <tr key={i} className="border-b border-gray-50">
                      <td className="py-2.5 font-mono text-xs text-gray-900">{m.name}</td>
                      <td className="py-2.5 text-xs text-gray-500">{m.source} → {m.target}</td>
                      <td className="py-2.5 text-center">
                        <span className={`text-xs px-1.5 py-0.5 rounded ${tierColors[m.tier]}`}>Tier {m.tier}</span>
                      </td>
                      <td className="py-2.5 text-center">
                        <span className={`text-xs px-1.5 py-0.5 rounded ${confColors[m.confidence]}`}>{m.confidence}</span>
                      </td>
                      <td className="py-2.5 text-center">
                        {m.flags.length > 0 ? (
                          <span title={m.flags.join(", ")} className="cursor-help">
                            <AlertTriangle className="w-3.5 h-3.5 text-amber-500 inline" />
                          </span>
                        ) : (
                          <Check className="w-3.5 h-3.5 text-green-500 inline" />
                        )}
                      </td>
                      <td className="py-2.5 text-right">
                        <div className="flex items-center justify-end gap-1">
                          <select
                            className="text-xs border border-gray-200 rounded px-1.5 py-1 bg-white text-gray-600"
                            value={overrides[m.name] || "confirm"}
                            onChange={(e) => setOverride(m.name, e.target.value)}
                          >
                            <option value="confirm">Confirm</option>
                            <option value="move">Move to...</option>
                            <option value="individual">Individual</option>
                          </select>
                          <button
                            onClick={() => setShowNotes(showNotes === m.name ? null : m.name)}
                            className="p-1 text-gray-400 hover:text-gray-600"
                            title="Add note"
                          >
                            <MessageSquare className="w-3.5 h-3.5" />
                          </button>
                        </div>
                        {showNotes === m.name && (
                          <textarea
                            className="mt-1 w-full text-xs border border-gray-200 rounded p-1.5 resize-none"
                            rows={2}
                            placeholder="Add reviewer note..."
                            value={notes[m.name] || ""}
                            onChange={(e) => setNotes(prev => ({ ...prev, [m.name]: e.target.value }))}
                          />
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Parameter differences */}
          {selectedGroup.paramDiffs.length > 0 && (
            <div className="bg-white rounded-lg border border-gray-200 p-4 mb-4">
              <p className="text-xs text-gray-500 uppercase tracking-wide mb-3">Parameter Differences</p>
              <p className="text-xs text-gray-400 mb-3">What varies across members — these become config entries in the template</p>
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-gray-500 uppercase tracking-wide border-b border-gray-100">
                    <th className="pb-2 font-medium">Parameter</th>
                    <th className="pb-2 font-medium">Values across members</th>
                  </tr>
                </thead>
                <tbody>
                  {selectedGroup.paramDiffs.map((p, i) => (
                    <tr key={i} className="border-b border-gray-50">
                      <td className="py-2 font-mono text-gray-700">{p.param}</td>
                      <td className="py-2 text-gray-500">
                        <div className="flex flex-wrap gap-1">
                          {p.values.map((v, j) => (
                            <span key={j} className="px-1.5 py-0.5 bg-gray-50 border border-gray-100 rounded font-mono">{v}</span>
                          ))}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Evidence */}
          <div className="bg-white rounded-lg border border-gray-200 p-4">
            <p className="text-xs text-gray-500 uppercase tracking-wide mb-3">Evidence — Why These Were Grouped</p>
            <div className="text-sm text-gray-600 space-y-2">
              <p>All {selectedGroup.count} mappings share the spine: <span className="font-mono text-xs bg-gray-50 px-1.5 py-0.5 rounded">{selectedGroup.spine.join(" → ")}</span></p>
              {selectedGroup.members.filter(m => m.tier === 1).length > 0 && (
                <p>{selectedGroup.members.filter(m => m.tier === 1).length} members are Tier 1 (parameter-only variation) — structurally identical, differ only by table names and column lists.</p>
              )}
              {selectedGroup.members.filter(m => m.tier === 2).length > 0 && (
                <p>{selectedGroup.members.filter(m => m.tier === 2).length} members are Tier 2 (minor structural variation) — same core flow with optional steps that can be handled via config flags.</p>
              )}
              <p className="text-xs text-gray-400 mt-2">Recommendation: 1 parameterized template + 1 YAML config with {selectedGroup.count} entries.</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
