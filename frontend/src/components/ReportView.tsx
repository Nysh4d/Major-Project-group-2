import { useState } from "react";
import SectionPanel from "./SectionPanel";

// Section display order per agent
const SECTION_ORDER: Record<string, string[]> = {
  architecture: [
    "overview",
    "module_architecture",
    "execution_flow",
    "object_model",
    "dependency_analysis",
    "risk_analysis",
  ],
  developer: [
    "purpose",
    "walkthrough",
    "components",
    "data_flow",
    "patterns",
  ],
  security: [
    "vulnerability_summary",
    "critical_findings",
    "code_quality_risks",
    "dependency_risks",
    "security_posture",
  ],
};

// Human-readable section titles
const SECTION_TITLES: Record<string, string> = {
  overview: "System Overview",
  module_architecture: "Module Architecture",
  execution_flow: "Execution Flow",
  object_model: "Object Model",
  dependency_analysis: "Dependencies",
  risk_analysis: "Risks",
  purpose: "What It Does",
  walkthrough: "How It Runs",
  components: "Components",
  data_flow: "Data Flow",
  patterns: "Patterns",
  vulnerability_summary: "Vulnerability Summary",
  critical_findings: "Critical Findings",
  code_quality_risks: "Code Quality Risks",
  dependency_risks: "Dependency Risks",
  security_posture: "Security Posture",
};

interface ReportViewProps {
  agent: string;
  report: {
    report_metadata?: Record<string, string>;
    ingestion_metadata?: Record<string, any>;
    sections: Record<string, string>;
  };
  analysisTime: number;
  modelUsed: string;
}

export default function ReportView({
  agent,
  report,
  analysisTime,
  modelUsed,
}: ReportViewProps) {
  const sections = report.sections || {};
  const order = SECTION_ORDER[agent] || Object.keys(sections);
  const availableSections = order.filter((key) => sections[key]);

  const [activeTab, setActiveTab] = useState(availableSections[0] || "");

  const meta = report.ingestion_metadata || {};

  return (
    <div className="mt-16 max-w-5xl mx-auto text-left">
      {/* Header */}
      <div className="flex items-center justify-between mb-6 flex-wrap gap-4">
        <h2 className="text-3xl font-bold text-teal-400">
          {agent === "architecture"
  ? "Architecture Report"
  : agent === "security"
    ? "Security Report"
    : "Developer Agent Report"}
        </h2>
        <div className="flex gap-3 text-sm text-gray-400">
          <span className="bg-gray-800 px-3 py-1 rounded-full">
            {analysisTime}s
          </span>
          <span className="bg-gray-800 px-3 py-1 rounded-full">
            {modelUsed}
          </span>
          {meta.total_source_files && (
            <span className="bg-gray-800 px-3 py-1 rounded-full">
              {meta.total_source_files} files
            </span>
          )}
          {meta.primary_language && (
            <span className="bg-gray-800 px-3 py-1 rounded-full">
              {meta.primary_language}
            </span>
          )}
        </div>
      </div>

      {/* Tab Bar */}
      <div className="flex gap-2 mb-6 overflow-x-auto pb-2">
        {availableSections.map((key) => (
          <button
            key={key}
            onClick={() => setActiveTab(key)}
            className={`px-4 py-2 rounded-lg text-sm font-medium whitespace-nowrap transition-all duration-200
              ${
                activeTab === key
                  ? "bg-teal-400/20 text-teal-400 border border-teal-400/40"
                  : "bg-gray-800 text-gray-400 border border-gray-700 hover:border-gray-500"
              }`}
          >
            {SECTION_TITLES[key] || key.replace(/_/g, " ")}
          </button>
        ))}
      </div>

      {/* Active Section */}
      {activeTab && sections[activeTab] && (
        <SectionPanel
          title={SECTION_TITLES[activeTab] || activeTab.replace(/_/g, " ")}
          content={sections[activeTab]}
        />
      )}
    </div>
  );
}
