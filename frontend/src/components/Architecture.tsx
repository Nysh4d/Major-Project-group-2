import { useEffect, useRef, useState } from "react";
import { renderPipeline } from "../d3/pipeline";
import FadeInSection from "./FadeInSection";

export default function Architecture() {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [activeStep, setActiveStep] = useState<number | null>(null);

  const SVG_HEIGHT = 220;

  // ---- Steps Data ----
  const steps = [
    {
      id: 1,
      label: ["User", "Input"],
      details: [
        "Upload .zip or GitHub repository",
        "Multi-language parsing",
        "Initial validation & scanning",
      ],
    },
    {
      id: 2,
      label: ["File", "Processor"],
      details: [
        "AST generation",
        "Dependency mapping",
        "Code normalization",
      ],
    },
    {
      id: 3,
      label: ["LangGraph", "Workflow"],
      details: [
        "Multi-agent orchestration",
        "Context propagation",
        "Intelligent reasoning engine",
      ],
    },
    {
      id: 4,
      label: ["Result", "Display"],
      details: [
        "Code explanation",
        "Performance insights",
        "Interactive visualization",
      ],
    },
  ];

  useEffect(() => {
    const handleResize = () => {
      if (!svgRef.current) return;

      const width = svgRef.current.clientWidth;

      renderPipeline(
        svgRef.current,
        width,
        SVG_HEIGHT,
        steps,
        (id: number) =>
          // ✅ Collapse if clicking same step again
          setActiveStep((prev) => (prev === id ? null : id))
      );
    };

    handleResize();
    window.addEventListener("resize", handleResize);

    return () => window.removeEventListener("resize", handleResize);
  }, []);

  return (
    <section id="architecture" className="pt-20 pb-12 px-6 bg-white">
      <FadeInSection>
        <h2 className="text-4xl md:text-5xl font-bold text-center mb-12 gradient-text">
          AI Architecture Flow
        </h2>

        <p className="text-gray-600 max-w-2xl mx-auto text-lg">
          Architecting a multi-stage pipeline to ingest and normalize raw source code.
          Executing deep-layer semantic evaluations using advanced AI-powered models.
          Synthesizing complex data patterns into high-impact, actionable intelligence.
          <br/>
          <br/>
        </p>

        <div className="max-w-6xl mx-auto">
          <svg ref={svgRef} width="100%" height={SVG_HEIGHT} />
        </div>

        {/* ---- Animated Technical Details Panel ---- */}
        <div
          className={`overflow-hidden transition-all duration-500 ease-in-out ${
            activeStep
              ? "max-h-[500px] opacity-100 translate-y-0 mt-8"
              : "max-h-0 opacity-0 -translate-y-4"
          }`}
        >
          {activeStep && (
            <div className="max-w-3xl mx-auto bg-gray-50 rounded-2xl p-8 shadow-lg">
              <h3 className="text-xl font-semibold mb-6 text-center">
                Technical Details
              </h3>

              <ul className="space-y-3 text-gray-700 text-sm">
                {steps
                  .find((step) => step.id === activeStep)
                  ?.details.map((item, index) => (
                    <li key={index} className="flex items-start gap-3">
                      <span className="w-2 h-2 mt-2 rounded-full bg-blue-500"></span>
                      {item}
                    </li>
                  ))}
              </ul>
            </div>
          )}
        </div>
      </FadeInSection>
    </section>
  );
}