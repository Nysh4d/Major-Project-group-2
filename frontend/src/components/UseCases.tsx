export default function UseCases() {
  return (
    <section id="use-cases" className="py-24 px-6 bg-gray-50">
      <div className="max-w-6xl mx-auto text-center">
        <h2 className="text-4xl md:text-5xl font-bold mb-6">
          Use <span className="gradient-text">Cases</span>
        </h2>

        <p className="text-gray-600 max-w-2xl mx-auto text-lg">
          Empowering developers, students, and educators with AI-driven
          insights tailored to their specific needs and goals.
        </p>
      </div>
      <div className="mt-20 grid md:grid-cols-3 gap-10 max-w-6xl mx-auto">
  
        {/* Card 1 */}
        <div className="bg-white rounded-2xl shadow-lg p-8 transition-all duration-300 hover:shadow-2xl hover:-translate-y-2">
            <div className="mb-6">
            <div className="w-14 h-14 bg-purple-500 text-white flex items-center justify-center rounded-xl text-2xl">
                📘
            </div>
            </div>

            <h3 className="text-2xl font-semibold mb-2">
            For Beginners
            </h3>

            <p className="text-gray-500 mb-6">
            Learning & Education
            </p>

            <ul className="space-y-3 text-gray-700 text-sm">
            <li>• Interactive code explanations with visual diagrams</li>
            <li>• Step-by-step debugging guidance</li>
            <li>• Pattern recognition for common structures</li>
            </ul>
        </div>

        {/* Card 2 */}
        <div className="bg-white rounded-2xl shadow-lg p-8 transition-all duration-300 hover:shadow-2xl hover:-translate-y-2">
            <div className="mb-6">
            <div className="w-14 h-14 bg-blue-500 text-white flex items-center justify-center rounded-xl text-2xl">
                ⚡
            </div>
            </div>

            <h3 className="text-2xl font-semibold mb-2">
            For Professionals
            </h3>

            <p className="text-gray-500 mb-6">
            Faster Development
            </p>

            <ul className="space-y-3 text-gray-700 text-sm">
            <li>• Rapid bug detection and root cause analysis</li>
            <li>• Performance optimization insights</li>
            <li>• Security vulnerability scanning</li>
            </ul>
        </div>

        {/* Card 3 */}
        <div className="bg-white rounded-2xl shadow-lg p-8 transition-all duration-300 hover:shadow-2xl hover:-translate-y-2">
            <div className="mb-6">
            <div className="w-14 h-14 bg-purple-500 text-white flex items-center justify-center rounded-xl text-2xl">
                🎓
            </div>
            </div>

            <h3 className="text-2xl font-semibold mb-2">
            For Educators
            </h3>

            <p className="text-gray-500 mb-6">
            Teaching Aid
            </p>

            <ul className="space-y-3 text-gray-700 text-sm">
            <li>• Automated grading and feedback</li>
            <li>• Student progress analytics</li>
            <li>• Curriculum-aligned explanations</li>
            </ul>
        </div>

      </div>

      {/* ================= Expert Personas ================= */}

      <div className="mt-32">
          <div className="text-center max-w-3xl mx-auto mb-16">
            <h2 className="text-4xl md:text-5xl font-bold mb-6">
            Expert <span className="gradient-text">Personas</span>
            </h2>

            <p className="text-gray-600 text-lg">
            Meet our specialized AI personas, each bringing unique expertise
            and perspective to deliver comprehensive code analysis.
            <br/>
            <br/>
            </p>
            
            <div className="grid md:grid-cols-3 gap-10 max-w-6xl mx-auto">
                {/* Card 1: Senior Architect */}
                <div className="bg-white rounded-2xl shadow-lg p-10 text-center transition-all duration-300 hover:shadow-2xl hover:-translate-y-2">
                    <div className="w-24 h-24 mx-auto mb-6 rounded-full bg-gray-100 flex items-center justify-center text-4xl">
                        👨‍💼
                    </div>

                    <h3 className="text-2xl font-semibold mb-2">
                        Senior Architect
                    </h3>

                    <p className="text-blue-600 font-medium mb-4">
                        Code Structure & Patterns
                    </p>

                    <p className="text-gray-600 text-sm">
                        Analyzes architectural patterns, design principles, and code
                        organization to ensure scalable and maintainable systems.
                    </p>
                </div>

                {/* Card 2: Security Expert */}
                <div className="bg-white rounded-2xl shadow-lg p-10 text-center transition-all duration-300 hover:shadow-2xl hover:-translate-y-2">
                    <div className="w-24 h-24 mx-auto mb-6 rounded-full bg-gray-100 flex items-center justify-center text-4xl">
                        🛡️
                    </div>

                    <h3 className="text-2xl font-semibold mb-2">
                        Security Expert
                    </h3>

                    <p className="text-red-500 font-medium mb-4">
                        Vulnerability Detection
                    </p>

                    <p className="text-gray-600 text-sm">
                        Identifies security vulnerabilities, compliance issues,
                        and implements robust protection strategies.
                    </p>
                </div>

                {/* Card 3: Security Expert */}
                <div className="bg-white rounded-2xl shadow-lg p-10 text-center transition-all duration-300 hover:shadow-2xl hover:-translate-y-2">
                    <div className="w-24 h-24 mx-auto mb-6 rounded-full bg-gray-100 flex items-center justify-center text-4xl">
                        ⚡
                    </div>

                    <h3 className="text-2xl font-semibold mb-2">
                        Senior Developer
                    </h3>

                    <p className="text-green-600 font-medium mb-4">
                        Optimization & Efficiency
                    </p>

                    <p className="text-gray-600 text-sm">
                        Focuses on performance tuning, resource optimization,
                        and scalability improvements.
                    </p>
                </div>

            </div>
          </div>
      </div>
    </section>
  );
}