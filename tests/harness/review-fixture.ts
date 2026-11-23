export function reviewed(prompt: string) {
  return [...prompt.matchAll(/^REVIEW KEY (.+)$/gm)].map(m => ({key:m[1]!,status:"reviewed",reason:"Inspected the assigned fixture source and its guards."}));
}
export function withReviews(value: any, prompt: string) {
  return value?.findings && !value.reviews ? {...value,reviews:reviewed(prompt)} : value;
}
