import { useRef, useState, useEffect } from "react";
import ReportView from "./ReportView";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

interface ReportData {
  report_metadata?: Record<string, string>;
  ingestion_metadata?: Record<string, any>;
  sections: Record<string, string>;
}

interface ApiResponse {
  agent: string;
  status: string;
  analysis_time_seconds: number;
  model_used: string;
  report: ReportData;
}

export default function Hero() {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const agentSectionRef = useRef<HTMLDivElement | null>(null);

  const [repoUrl, setRepoUrl] = useState("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null);

  const [loading, setLoading] = useState(false);
  const [apiResponse, setApiResponse] = useState<ApiResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const agents = [
    {
      id: "architecture",
      title: "Senior Architect",
      description: "Explains system design, modules, dependencies & object model.",
    },
    {
      id: "developer",
      title: "Developer Agent",
      description: "Walks through how the code actually works, step by step.",
    },
    {
      id: "security",
      title: "Security Agent",
      description: "Scans for vulnerabilities, secrets, and security risks.",
    },
  ];

  const hasInput = repoUrl.trim() !== "" || selectedFile !== null;
  const canGenerate = hasInput && selectedAgent !== null;

  // Auto-scroll to agent picker when input is provided
  useEffect(() => {
    if (!hasInput) return;

    const element = agentSectionRef.current;
    if (!element) return;

    const timeout = setTimeout(() => {
      const yOffset = -340;
      const y =
        element.getBoundingClientRect().top +
        window.pageYOffset +
        yOffset;

      window.scrollTo({ top: y, behavior: "smooth" });
    }, 200);

    return () => clearTimeout(timeout);
  }, [hasInput]);

  const handleGenerate = async () => {
    if (!selectedFile || !selectedAgent) return;

    setLoading(true);
    setError(null);
    setApiResponse(null);

    try {
      const formData = new FormData();
      formData.append("file", selectedFile);
      formData.append("agent", selectedAgent);

      const response = await fetch(`${API_BASE}/run-agent`, {
        method: "POST",
        body: formData,
      });

      if (!response.ok) {
        const detail = await response.text();
        throw new Error(detail || `Server error ${response.status}`);
      }

      const data: ApiResponse = await response.json();
      setApiResponse(data);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Something went wrong while generating analysis."
      );
    } finally {
      setLoading(false);
    }
  };

  return (
    <section
      id="home"
      className="bg-black text-white py-28 px-6 text-center"
    >
      <h1 className="text-5xl md:text-6xl font-bold gradient-text mb-6">
        NeuroSense
      </h1>

      <h4 className="text-3xl md:text-4xl font-bold gradient-text mb-6">
        AI-Powered CodeBase Analyzer & Explainer
      </h4>

      <p className="text-gray-400 max-w-2xl mx-auto mb-12">
        Upload your repository and let AI experts analyze it.
      </p>

      {/* Stats */}
      <div className="flex justify-center gap-6 flex-wrap mb-12">
        {["99.9% Accuracy", "120+ Models", "24ms Latency"].map((stat) => (
          <div
            key={stat}
            className="bg-gray-800 px-6 py-3 rounded-full text-sm shadow-md
            transition-all duration-300 transform-gpu
            hover:scale-105 hover:-translate-y-1
            hover:shadow-2xl hover:bg-gray-700
            cursor-pointer"
          >
            {stat}
          </div>
        ))}
      </div>

      {/* Upload Section */}
      <div className="max-w-xl mx-auto border-2 border-dashed border-gray-600 rounded-[20px] p-10 text-gray-300 transition-all duration-300 hover:border-teal-400">
        <input
          type="file"
          accept=".zip"
          ref={fileInputRef}
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file && file.name.endsWith(".zip")) {
              setSelectedFile(file);
              setRepoUrl("");
            }
          }}
        />

        <div className="flex flex-col items-center gap-4">
          <p className="text-lg font-medium">
            Upload your project (.zip) or paste GitHub repo
          </p>

          <button
            onClick={() => fileInputRef.current?.click()}
            className="px-6 py-2 rounded-full bg-gradient-to-r from-blue-500 via-teal-400 to-green-400 text-white font-medium shadow-md hover:scale-105 transition-all duration-300"
          >
            Select .zip File
          </button>

          {selectedFile && (
            <p className="text-sm text-teal-400">
              Selected: {selectedFile.name}
            </p>
          )}

          <div className="text-gray-500 text-sm">OR</div>

          <input
            type="url"
            placeholder="https://github.com/username/repository"
            value={repoUrl}
            onChange={(e) => {
              setRepoUrl(e.target.value);
              setSelectedFile(null);
            }}
            className="w-full px-4 py-2 rounded-lg bg-gray-800 border border-gray-600 focus:outline-none focus:border-teal-400 text-sm"
          />
        </div>
      </div>

      {/* Agent Section */}
      <div
        ref={agentSectionRef}
        className={`transition-all duration-700 ease-out overflow-hidden ${
          hasInput
            ? "opacity-100 translate-y-0 mt-20"
            : "opacity-0 -translate-y-10 h-0"
        }`}
      >
        {hasInput && (
          <>
            <h3 className="text-2xl font-semibold mb-8">
              Choose Your Expert Agent
            </h3>

            <div className="grid md:grid-cols-2 gap-6 max-w-2xl mx-auto">
              {agents.map((agent) => (
                <div
                  key={agent.id}
                  onClick={() => setSelectedAgent(agent.id)}
                  className={`p-6 rounded-xl border cursor-pointer transition-all duration-300
                  ${
                    selectedAgent === agent.id
                      ? "border-teal-400 bg-gray-800 shadow-xl scale-105"
                      : "border-gray-700 hover:border-teal-400 hover:scale-105"
                  }`}
                >
                  <h4 className="text-xl font-semibold mb-2">{agent.title}</h4>
                  <p className="text-gray-400 text-sm">{agent.description}</p>
                </div>
              ))}
            </div>

            <div className="mt-12">
              <button
                onClick={handleGenerate}
                disabled={!canGenerate || loading}
                className={`px-10 py-4 rounded-lg font-semibold text-white transition-all duration-300
                ${
                  canGenerate && !loading
                    ? "bg-gradient-to-r from-teal-400 to-blue-500 hover:scale-105"
                    : "bg-gray-600 cursor-not-allowed"
                }`}
              >
                {loading ? (
                  <span className="flex items-center gap-3 justify-center">
                    <svg
                      className="animate-spin h-5 w-5 text-white"
                      xmlns="http://www.w3.org/2000/svg"
                      fill="none"
                      viewBox="0 0 24 24"
                    >
                      <circle
                        className="opacity-25"
                        cx="12"
                        cy="12"
                        r="10"
                        stroke="currentColor"
                        strokeWidth="4"
                      />
                      <path
                        className="opacity-75"
                        fill="currentColor"
                        d="M4 12a8 8 0 018-8v8H4z"
                      />
                    </svg>
                    Analyzing...
                  </span>
                ) : (
                  "Generate Analysis"
                )}
              </button>
            </div>

            {error && (
              <div className="mt-6 max-w-xl mx-auto bg-red-900/30 border border-red-500/40 rounded-lg px-6 py-4 text-red-400 text-sm">
                {error}
              </div>
            )}
          </>
        )}
      </div>

      {/* Report */}
      {apiResponse && (
        <ReportView
          agent={apiResponse.agent}
          report={apiResponse.report}
          analysisTime={Math.round(apiResponse.analysis_time_seconds)}
          modelUsed={apiResponse.model_used}
        />
      )}
    </section>
  );
}
