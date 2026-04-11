import Card from "./Card";
import FadeInSection from "./FadeInSection";

const features = [
  {
    icon: "🧠",
    title: "Multi-Agent Personas",
    metric: "3 Expert Personas",
    description: "Specialized AI experts including Senior Architect, Security Expert, and Performance Specialist work together to analyze your code from multiple perspectives."
  },
  {
    icon: "📊",
    title: "Probabilistic Bug Prediction",
    metric: "70% Accuracy",
    description: "Advanced ML models predict potential bugs with 70% accuracy, helping you catch issues before they reach production."
  },
  {
    icon: "</>",
    title: "Cross-Language Dependency Mapping",
    metric: "90% Coverage",
    description: "Comprehensive analysis across multiple programming languages with 90% coverage, understanding complex interdependencies."
  },
  {
    icon: "⚡",
    title: "Visual Code Archaeology",
    metric: "40% Faster",
    description: "Revolutionary visualization of code evolution and structure, improving comprehension speed by 40% through interactive diagrams."
  },
  {
    icon: "📄",
    title: "Adaptive Explanation Depth",
    metric: "Depth Levels",
    description: "Tailored explanations that adjust to your expertise level, from beginner-friendly overviews to deep technical analysis."
  },
  {
    icon: "🔐",
    title: "Security Vulnerability Detection",
    metric: "OWASP Compliant",
    description: "Real-time identification of security risks and compliance issues with actionable remediation suggestions."
  }
];

export default function Features() {
  return (
    <section id="features" className="py-24 px-6 bg-gray-50">
      <div className="max-w-7xl mx-auto">
        <h2 className="text-4xl font-bold text-center mb-16 gradient-text">
          Powerful AI Capabilities
        </h2>

        <div className="grid md:grid-cols-3 gap-8">
          {features.map((feature, index) => (
            <FadeInSection key={index}>
                <Card>
                <div className="flex items-center justify-between mb-4">
                    {/* Icon */}
                    <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-blue-500 to-purple-500 flex items-center justify-center text-white text-xl shadow-md">
                    {feature.icon}
                    </div>

                    {/* Metric */}
                    <span className="text-xs font-semibold px-3 py-1 rounded-full bg-gray-100 text-gray-700">
                    {feature.metric}
                    </span>
                </div>

                {/* Title */}
                <h3 className="text-lg font-semibold mb-2">
                    {feature.title}
                </h3>

                {/* Description */}
                <p className="text-sm text-gray-600 leading-relaxed">
                    {feature.description}
                </p>
                </Card>
            </FadeInSection>
            ))}
        </div>
      </div>
    </section>
  );
}