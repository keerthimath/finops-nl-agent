// "Pin question" under an answer. Pins live in this browser (localStorage), per signed-in user.
// props: { question: string, user: string }. PinnedQuestions.jsx reads the same key.
import { Button } from "@/components/ui/button"
import { Pin, PinOff } from "lucide-react"
import { useEffect, useState } from "react"

const MAX_PINS = 50
const storageKey = () => `finops-pins:${props.user || "me"}`

function loadPins() {
  try {
    const pins = JSON.parse(window.localStorage.getItem(storageKey()) || "[]")
    return Array.isArray(pins) ? pins.filter((q) => typeof q === "string") : []
  } catch (e) {
    return []
  }
}

function savePins(pins) {
  try {
    window.localStorage.setItem(storageKey(), JSON.stringify(pins.slice(0, MAX_PINS)))
  } catch (e) {
    // storage blocked (private mode): pinning just doesn't stick
  }
  window.dispatchEvent(new Event("finops-pins"))
}

export default function PinButton() {
  const [pinned, setPinned] = useState(false)

  useEffect(() => {
    const sync = () => setPinned(loadPins().includes(props.question))
    sync()
    window.addEventListener("finops-pins", sync)
    window.addEventListener("storage", sync)
    return () => {
      window.removeEventListener("finops-pins", sync)
      window.removeEventListener("storage", sync)
    }
  }, [])

  const toggle = () => {
    const others = loadPins().filter((q) => q !== props.question)
    savePins(pinned ? others : [props.question, ...others])
  }

  return (
    <Button variant="ghost" size="sm" className="text-muted-foreground" onClick={toggle}>
      {pinned ? <PinOff className="h-4 w-4 mr-1" /> : <Pin className="h-4 w-4 mr-1" />}
      {pinned ? "Unpin question" : "Pin question"}
    </Button>
  )
}
