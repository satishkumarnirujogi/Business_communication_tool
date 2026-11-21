export type Lang = "en" | "de";

export const TEXT = {
  en: {
    greeting: "Hi! I can help you book, move or cancel an appointment, and answer questions about the clinic.",
    placeholder: "Type your message…",
    send: "Send",
    assistant: "Virtual assistant",
    online: "Usually replies instantly",
    human: "You're chatting with our team",
    notEmergency: "Not for emergencies. In an emergency call 112.",
    chips: ["Book appointment", "Opening hours", "Services and prices", "Cancel or reschedule"],
    chipMessages: [
      "I'd like to book an appointment.",
      "What are your opening hours?",
      "Which services do you offer and what do they cost?",
      "I'd like to cancel or reschedule my appointment.",
    ],
    pickTime: (label: string, doctor: string) => `I'd like ${label} with ${doctor}.`,
    yes: "Yes, confirm",
    change: "Change something",
    yesMessage: "Yes, please confirm.",
    changeMessage: "I'd like to change something.",
    booked: "Appointment confirmed",
    reference: "Reference",
    addToCalendar: "Add to calendar",
    sources: "Sources",
    status: {
      search_clinic_knowledge: "Checking our information…",
      check_available_slots: "Checking availability…",
      book_appointment: "Preparing your booking…",
      reschedule_appointment: "Preparing the change…",
      cancel_appointment: "Checking your booking…",
      lookup_appointment: "Looking up your appointments…",
      list_services: "Checking our services…",
      list_doctors: "Checking our doctors…",
    } as Record<string, string>,
    error: "Sorry, something went wrong. Please try again.",
    close: "Close chat",
    newChat: "New conversation",
    team: "Clinic team",
  },
  de: {
    greeting: "Hallo! Ich helfe Ihnen, Termine zu buchen, zu verschieben oder abzusagen, und beantworte Fragen zur Praxis.",
    placeholder: "Ihre Nachricht…",
    send: "Senden",
    assistant: "Virtueller Assistent",
    online: "Antwortet meist sofort",
    human: "Sie schreiben mit unserem Team",
    notEmergency: "Nicht für Notfälle. Im Notfall rufen Sie bitte die 112.",
    chips: ["Termin buchen", "Öffnungszeiten", "Leistungen und Preise", "Absagen oder verschieben"],
    chipMessages: [
      "Ich möchte einen Termin buchen.",
      "Wie sind Ihre Öffnungszeiten?",
      "Welche Leistungen bieten Sie an und was kosten sie?",
      "Ich möchte meinen Termin absagen oder verschieben.",
    ],
    pickTime: (label: string, doctor: string) => `Ich hätte gern ${label} bei ${doctor}.`,
    yes: "Ja, bestätigen",
    change: "Etwas ändern",
    yesMessage: "Ja, bitte bestätigen.",
    changeMessage: "Ich möchte etwas ändern.",
    booked: "Termin bestätigt",
    reference: "Referenz",
    addToCalendar: "Zum Kalender hinzufügen",
    sources: "Quellen",
    status: {
      search_clinic_knowledge: "Ich schaue in unseren Informationen nach…",
      check_available_slots: "Ich prüfe freie Termine…",
      book_appointment: "Ich bereite Ihre Buchung vor…",
      reschedule_appointment: "Ich bereite die Änderung vor…",
      cancel_appointment: "Ich prüfe Ihren Termin…",
      lookup_appointment: "Ich suche Ihre Termine…",
      list_services: "Ich prüfe unsere Leistungen…",
      list_doctors: "Ich prüfe unsere Ärztinnen und Ärzte…",
    } as Record<string, string>,
    error: "Entschuldigung, da ist etwas schiefgegangen. Bitte versuchen Sie es erneut.",
    close: "Chat schließen",
    newChat: "Neues Gespräch",
    team: "Praxisteam",
  },
} as const;

export function initialLang(available: string[]): Lang {
  const fromUrl = new URLSearchParams(location.search).get("lang");
  const wanted = (fromUrl || navigator.language || "en").slice(0, 2).toLowerCase();
  const ok = available.filter((l): l is Lang => l === "en" || l === "de");
  return (ok.includes(wanted as Lang) ? wanted : ok[0] ?? "en") as Lang;
}
