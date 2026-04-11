"use client"

import { useRef, useState } from "react"
import emailjs from "@emailjs/browser"

export default function Contact() {
  const form = useRef<HTMLFormElement>(null)
  const [loading, setLoading] = useState(false)
  const [success, setSuccess] = useState(false)

  const sendEmail = (e: React.FormEvent) => {
    e.preventDefault()

    if (!form.current) return

    setLoading(true)

    emailjs
      .sendForm(
        "service_rdbht0b",      // 🔹 service id
        "template_t9ocq9b",     // 🔹 template id
        form.current,
        "7Q-Zc5p8o9HftIEWz"       // 🔹 public key
      )
      .then(
        () => {
          setLoading(false)
          setSuccess(true)
          form.current?.reset()
        },
        () => {
          setLoading(false)
          alert("Something went wrong. Try again.")
        }
      )
  }

  return (
    <section id="contact" className="py-28 px-6 bg-gradient-to-b from-[#1e2b55] to-[#0f1a3a] text-white">
      <div className="max-w-6xl mx-auto">

        {/* Header */}
        <div className="text-center mb-20">
          <h2 className="text-4xl md:text-5xl font-bold mb-6">
            Get <span className="bg-gradient-to-r from-blue-400 to-teal-400 bg-clip-text text-transparent">Involved</span>
          </h2>

          <p className="text-gray-300 max-w-2xl mx-auto text-lg">
            Join our community of developers, contribute to the project,
            or get in touch with our team.
          </p>
        </div>

        <div className="grid md:grid-cols-2 gap-12">

          {/* Contact Form */}
          <div className="bg-white/10 backdrop-blur-md border border-white/20 rounded-2xl p-8 shadow-xl">

            <h3 className="text-2xl font-semibold mb-6">
              💬 Send us a Message
            </h3>

            <form ref={form} onSubmit={sendEmail} className="space-y-5">

              <input
                type="text"
                name="from_name"
                required
                placeholder="Your Name"
                className="w-full p-4 rounded-lg bg-white/10 border border-white/20 placeholder-gray-300 focus:outline-none focus:ring-2 focus:ring-teal-400"
              />

              <input
                type="email"
                name="from_email"
                required
                placeholder="Your Email"
                className="w-full p-4 rounded-lg bg-white/10 border border-white/20 placeholder-gray-300 focus:outline-none focus:ring-2 focus:ring-teal-400"
              />

              <textarea
                name="message"
                required
                rows={5}
                placeholder="Your Message"
                className="w-full p-4 rounded-lg bg-white/10 border border-white/20 placeholder-gray-300 focus:outline-none focus:ring-2 focus:ring-teal-400"
              />

              <button
                type="submit"
                disabled={loading}
                className="w-full py-4 rounded-lg bg-gradient-to-r from-blue-500 to-teal-500 font-semibold hover:opacity-90 transition-all duration-300"
              >
                {loading ? "Sending..." : "Send Message"}
              </button>

              {success && (
                <p className="text-green-400 text-center">
                  ✅ Message sent successfully!
                </p>
              )}
            </form>
          </div>

          {/* Right Side Cards */}
          <div className="space-y-8">

            <div className="bg-white/10 backdrop-blur-md border border-white/20 rounded-2xl p-8 shadow-xl">
              <h3 className="text-2xl font-semibold mb-2">
                🐙 Contribute on GitHub
              </h3>
              <p className="text-gray-300 mb-6">
                Open source and welcoming contributors
              </p>
              <a
                href="https://github.com/yourrepo"
                target="_blank"
                rel="noopener noreferrer"
                className="block text-center py-3 rounded-lg bg-white text-blue-600 font-semibold hover:bg-gray-200 transition duration-300"
              >
                View Repository
              </a>
            </div>

            <div className="bg-white/10 backdrop-blur-md border border-white/20 rounded-2xl p-8 shadow-xl">
              <h3 className="text-2xl font-semibold mb-2">
                📧 Direct Contact
              </h3>
              <p className="text-gray-300 mb-6">
                Get in touch with the team
              </p>
              <div className="bg-white text-green-600 font-semibold py-3 px-4 rounded-lg text-center">
                team@neurosense.ai
              </div>
            </div>

          </div>
        </div>
      </div>
    </section>
  )
}